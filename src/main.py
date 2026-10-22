"""服务入口：装配并监护全部异步任务。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from src.bot.auth import AuthManager
from src.bot.commands import CommandHandler
from src.bot.telegram import BotRunner, TelegramApi, run_sender
from src.core.models import StandardEvent
from src.core.pipeline import PipelineRunner
from src.core.renderer import AlertMessage
from src.notify import Notifier, build_notifiers
from src.settings import Settings
from src.snapshots.collector import (
    SnapshotCollector,
    _current_bucket_start,
)
from src.snapshots.commands import SnapshotCommandHandler
from src.snapshots.renderer import render_snapshot_summary
from src.sources.data_api_client import DataApiClient
from src.sources.gamma_client import GammaClient
from src.sources.rtds_client import RtdsClient
from src.storage import Storage
from src.utils.logging import setup_logging
from src.utils.retry import backoff_delay

logger = logging.getLogger("main")


async def heartbeat_writer(stop_event: asyncio.Event) -> None:
    """每 15s 写心跳文件，供容器 healthcheck。"""
    path = Path("data/heartbeat")
    while not stop_event.is_set():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass


async def health_watcher(
    health: dict[str, Any],
    rtds: RtdsClient,
    storage: Storage,
    stop_event: asyncio.Event,
) -> None:
    """刷新运行状态（供 /status 读取）。"""
    local_tz = timezone(timedelta(hours=8))
    while not stop_event.is_set():
        health["rtds_connected"] = rtds.connected
        raw = await storage.get_state("l2_last_run")
        if raw:
            dt = datetime.fromisoformat(raw).astimezone(local_tz)
            health["l2_last_run"] = dt.strftime("%m-%d %H:%M")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=20)
        except asyncio.TimeoutError:
            pass


async def prune_loop(storage: Storage, settings: Settings, stop_event: asyncio.Event) -> None:
    """每日清理：过期事件、过期推送记录、过期快照，并 VACUUM 回收文件空间。"""
    events_days = int(settings.get("events.retention_days", 180))
    notifications_days = int(
        settings.get("events.notifications_retention_days", 30)
    )
    snapshot_days = int(settings.snapshot.get("retention_days", 90))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=86400)
        except asyncio.TimeoutError:
            await storage.prune_events(events_days)
            await storage.prune_notifications(notifications_days)
            await storage.prune_snapshots(snapshot_days)
            await storage.vacuum()
            logger.info(
                "数据清理完成（事件 %d 天 / 推送记录 %d 天 / 快照 %d 天 / 已 VACUUM）",
                events_days,
                notifications_days,
                snapshot_days,
            )


# ── 市场快照采集与推送 ──────────────────────────────────────────────────


async def snapshot_collect_loop(
    collector: SnapshotCollector,
    markets: list[Any],
    stop_event: asyncio.Event,
    collect_state: dict[str, float] | None = None,
    interval_seconds: int = 600,
) -> None:
    """固定间隔采集（默认 10 分钟一轮，桶内可重试）；启动先跑一轮。

    旧版只在 2h 桶边界 +2s 采一次，低活跃市场新桶头几秒没有价点时当轮
    早退且永不补齐，最新桶永久冻结（10-20 事故）。改为 10 分钟一轮后：
    - 新桶头几分钟没价点，下一轮（10 分钟内）即可补上；
    - 每轮都用已拉回的历史价点补齐缺失闭合桶（漏桶自愈）；
    - 每轮都幂等结算上一闭合桶量、在宽限窗口内记录新桶起点基线。

    collect_state 记录最近一轮采集完成的时间戳。
    """
    state = collect_state if collect_state is not None else {}
    # 启动首轮：collect 内部按"距桶起点的时间"自行决定是否记基线（超宽限不记），
    # 不再依赖调用方区分边界轮/中途轮。
    await _snapshot_collect_round(collector, markets)
    state["round_done_ts"] = float(time.time())
    while not stop_event.is_set():
        now = int(time.time())
        delay = interval_seconds - (now % interval_seconds)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay + 2)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        await _snapshot_collect_round(collector, markets)
        state["round_done_ts"] = float(time.time())


async def _snapshot_collect_round(
    collector: SnapshotCollector,
    markets: list[Any],
) -> None:
    """采集一轮全部市场；单市场失败跳过不影响整轮。

    collect() 幂等：同一桶内重复执行只补全不污染，补算与基线落库由
    collector 内部按时间窗口自行判定。
    """
    for m in markets:
        try:
            cid = m.condition_id
            yes_token: str | None = None
            resolved = await collector.resolve_market(m.slug)
            if resolved:
                if not cid:
                    cid, yes_token = resolved
                else:
                    yes_token = resolved[1]
            if not cid or not yes_token:
                logger.warning(
                    "快照市场标识解析失败，跳过: %s（cid=%s token=%s）",
                    m.slug_id,
                    bool(cid),
                    bool(yes_token),
                )
                continue
            await collector.collect(
                condition_id=cid,
                yes_token_id=yes_token,
                slug=m.slug,
                slug_id=m.slug_id,
                fv_initial=m.fv_initial,
                fv_end_date=m.fv_end_date,
            )
        except Exception as exc:  # noqa: BLE001 - 单市场失败跳过
            logger.warning("快照采集失败 %s: %s", m.slug_id, exc)


def _parse_snapshot_schedule(schedule: list[str]) -> list[tuple[int, int, str]]:
    """解析北京时间 HH:MM 列表 → [(hour, minute, label)]；24:00 归一为 00:00。

    同一时刻（含 00:00 与 24:00 并存）只保留一条，防止午夜/重复配置双推。
    """
    out: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for item in schedule:
        text = str(item).strip()
        try:
            parts = text.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            logger.warning("快照推送时间格式错误，跳过: %s", text)
            continue
        if hour == 24:
            hour, label = 0, "24:00"
        else:
            label = text
        key = (hour, minute)
        if key in seen:
            continue
        seen.add(key)
        out.append((hour, minute, label))
    return out


async def snapshot_push_loop(
    storage: Storage,
    settings: Settings,
    trigger_push: Any,
    stop_event: asyncio.Event,
) -> None:
    """按配置的北京时间整点推送总览；已推过的整点不重推，错过的整点不补推。

    重复内容由 trigger_snapshot_push 的 dedup 再去一层（2h 桶配整点推送时，
    奇数点内容相同会被抑制）。
    """
    push_cfg = settings.snapshot_push
    schedule = _parse_snapshot_schedule(push_cfg.get("schedule", []) or [])
    tz_offset = int(push_cfg.get("timezone_offset_hours", 8))
    if not schedule:
        logger.info("快照推送时间表为空，推送循环退出")
        return

    local_tz = timezone(timedelta(hours=tz_offset))
    while not stop_event.is_set():
        now_bj = datetime.now(local_tz)
        pushed_any = False
        for hour, minute, label in schedule:
            if (now_bj.hour, now_bj.minute) != (hour, minute):
                continue
            date_key = now_bj.strftime("%Y-%m-%d")
            state_key = f"snap_push:{date_key}:{label}"
            already = await storage.get_state(state_key)
            if already:
                continue
            try:
                await trigger_push()
            except Exception as exc:  # noqa: BLE001 - 推送失败不影响下次
                logger.error("整点推送失败 %s: %s", label, exc)
                continue
            await storage.set_state(state_key, "1")
            logger.info("整点快照已推送: %s %s", date_key, label)
            pushed_any = True
        if pushed_any:
            # 推完一轮后等下一个整点再查
            await asyncio.sleep(60)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


def supervised(
    name: str,
    factory: Callable[[], Any],
    stop_event: asyncio.Event,
) -> Any:
    """任务监护：异常退出后自动重启，stop_event 置位后正常退出。"""

    async def wrapper() -> None:
        attempt = 0
        while not stop_event.is_set():
            task = asyncio.create_task(factory())
            try:
                await task
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 监护边界
                logger.warning("任务 %s 异常退出: %s", name, exc)
            attempt += 1
            delay = backoff_delay(attempt, 1.0, 30.0)
            logger.info("任务 %s %.1fs 后重启", name, delay)
            await asyncio.sleep(delay)

    return wrapper()


async def amain(config_path: str) -> None:
    load_dotenv()
    settings = Settings.load(config_path)
    setup_logging(
        settings.log_level,
        log_file=settings.get("logging.file"),
        max_mb=int(settings.get("logging.max_mb", 10)),
        backups=int(settings.get("logging.backups", 5)),
    )
    logger.info("服务启动")

    # 存储
    storage = Storage("data/monitor.db")
    await storage.init()
    seeded = await storage.seed_wallets(settings.watchlist_seeds)
    if seeded:
        logger.info("配置种子新增监控地址 %d 个", seeded)

    endpoints = settings.section("endpoints")
    polling = settings.section("polling")

    # 客户端
    rtds = RtdsClient(
        str(endpoints.get("rtds")),
        heartbeat_seconds=int(polling.get("rtds_heartbeat_seconds", 15)),
        reconnect_min_seconds=float(polling.get("reconnect_min_seconds", 1)),
        reconnect_max_seconds=float(polling.get("reconnect_max_seconds", 30)),
    )
    data_api = DataApiClient(
        str(endpoints.get("data_api")),
        interval_seconds=int(polling.get("reconciliation_interval_seconds", 60)),
        timeout_seconds=int(polling.get("request_timeout_seconds", 10)),
        max_concurrency=int(polling.get("max_concurrent_requests", 5)),
        page_limit=int(polling.get("cold_start_fetch_limit", 50)),
    )
    gamma = GammaClient(
        str(endpoints.get("gamma_api")),
        timeout_seconds=int(polling.get("request_timeout_seconds", 10)),
        cache_ttl_hours=int(settings.get("market_cache.ttl_hours", 24)),
    )

    event_queue: asyncio.Queue[StandardEvent] = asyncio.Queue(maxsize=10000)
    message_queue: asyncio.Queue[AlertMessage] = asyncio.Queue(maxsize=1000)
    reconcile_event = asyncio.Event()
    stop_event = asyncio.Event()

    # 冷启动基线（重试到成功，避免首启把历史当新交易推送）
    wallets = await storage.list_wallets(active_only=True)
    cold_attempt = 0
    while not stop_event.is_set():
        try:
            await data_api.cold_start(wallets, storage)
            break
        except Exception as exc:  # noqa: BLE001 - 冷启动失败必须重试
            cold_attempt += 1
            delay = backoff_delay(cold_attempt, 2.0, 60.0)
            logger.warning("冷启动基线建立失败: %s，%.0fs 后重试", exc, delay)
            await asyncio.sleep(delay)

    pipeline = PipelineRunner(event_queue, message_queue, storage, gamma, settings)
    health: dict[str, Any] = {"rtds_connected": False, "l2_last_run": "-"}

    # ── 市场快照：采集器 + 推送调度 ─────────────────────────────────
    snapshot_push_cfg = settings.snapshot_push
    snapshot_cfg = settings.snapshot
    markets = settings.markets
    if markets:
        logger.info("快照市场 %d 个: %s", len(markets), [m.slug_id for m in markets])
    else:
        logger.info("快照市场为空（config/markets.toml 未配置），仅注册推送循环")
    snap_collector = SnapshotCollector(
        storage,
        gamma_host=str(endpoints.get("gamma_api", "https://gamma-api.polymarket.com")),
        clob_host=str(endpoints.get("clob_api", "https://clob.polymarket.com")),
        timeout=float(snapshot_cfg.get("request_timeout_seconds", 15)),
        price_fidelity_minutes=int(snapshot_cfg.get("price_fidelity_minutes", 60)),
    )
    snap_health: dict[str, Any] = {"snap_last_push": "-"}
    snap_auth: AuthManager | None = None
    snap_runner: BotRunner | None = None
    # 快照采集完成状态：round_done_ts = 最近一轮采集完成时刻（供整点推送等待）
    collect_state: dict[str, float] = {"round_done_ts": 0.0}
    # 快照推送通道列表（见下方装配；可 telegram + ntfy 同时启用）
    snap_notifiers: list[Notifier] = []

    async def trigger_snapshot_push() -> None:
        """读取最近完整桶 → 渲染总览 → 推送到快照 Bot 白名单。"""
        import hashlib

        now_bucket = _current_bucket_start()
        bucket_seconds = int(snapshot_cfg.get("bucket_seconds", 7200))
        # 推送前主动补算上一闭合桶份额（当前累计 - 桶起点基线），幂等可重复。
        # 注意：markets.toml 允许只填 slug（condition_id 留空），必须先用
        # 采集器解析出的 cid 补算，否则空 cid 请求 Gamma 会静默失败（旧 bug）。
        for m in markets:
            try:
                cid = m.condition_id
                if not cid:
                    resolved = await snap_collector.resolve_market(m.slug)
                    if resolved:
                        cid = resolved[0]
                if cid:
                    await snap_collector.backfill_prev_bucket(cid)
            except Exception as exc:  # noqa: BLE001 - 单市场补算失败降级
                logger.warning("推送前补算失败 %s: %s", m.slug_id, exc)
        rows = await storage.latest_complete_buckets(now_bucket)
        if not rows:
            logger.info("快照推送跳过：暂无完整桶数据")
            return
        # 数据陈旧守卫：最新桶落后超过阈值（默认 0=不跳过，仅标题提示延迟）
        max_stale = int(snapshot_push_cfg.get("max_stale_buckets", 0) or 0)
        lag_buckets = max(
            (now_bucket - int(r["bucket_ts"])) // bucket_seconds for r in rows
        )
        if max_stale > 0 and lag_buckets > max_stale:
            logger.info(
                "快照推送跳过：数据落后 %d 个桶（阈值 %d），疑似采集异常",
                lag_buckets,
                max_stale,
            )
            return
        # 推送前实时取各市场当前盘面价（失败降级用闭合桶收盘价）
        try:
            live = await snap_collector.fetch_live_prices(rows)
            for row in rows:
                cid = str(row.get("condition_id") or "")
                if cid in live:
                    row["current_price"] = live[cid]
        except Exception as exc:  # noqa: BLE001 - 实时价失败不影响推送
            logger.warning("快照实时价拉取失败，回退收盘价: %s", exc)
        # 上一闭合桶份额：直接读库（基线差按桶精确落库；
        # NULL = 该桶基线缺失（未知），渲染显示"-"，不再误导为 0 成交）
        for row in rows:
            shares = row.get("volume_shares")
            row["volume_shares_display"] = (
                float(shares) if shares is not None else -1.0
            )
        alert = render_snapshot_summary(
            rows,
            tz_offset_hours=int(snapshot_push_cfg.get("timezone_offset_hours", 8)),
            bucket_seconds=bucket_seconds,
            now_bucket=now_bucket,
        )
        if alert is None:
            return
        # 正文去重：2h 桶配整点推送时，奇数点内容天然相同；采集停摆时旧桶
        # 也会被反复推。内容与上一条完全一致则抑制（默认开，可配 dedup=false）。
        if snapshot_push_cfg.get("dedup", True):
            body_hash = hashlib.sha1(alert.html.encode("utf-8")).hexdigest()
            if await storage.get_state("snap_push:body_hash") == body_hash:
                logger.info("快照推送跳过：内容与上一条完全一致（去重）")
                return
        # 经全部推送通道投递（telegram 白名单多 chat / ntfy topic，可同时启用）
        send_failed = False
        if snap_notifiers:
            for notifier in snap_notifiers:
                try:
                    await notifier.send(alert.html)
                except Exception as exc:  # noqa: BLE001 - 单通道失败不影响其他
                    send_failed = True
                    logger.error("快照推送失败(%s): %s", notifier.describe(), exc)
        else:
            send_failed = True
            logger.warning("快照推送通道未配置，总览未推送（仅采集落库）")
        # 只有投递成功才记录去重指纹，失败的下轮允许重试
        if not send_failed and snapshot_push_cfg.get("dedup", True):
            await storage.set_state("snap_push:body_hash", body_hash)
        snap_health["snap_last_push"] = datetime.now(
            timezone(timedelta(hours=8))
        ).strftime("%m-%d %H:%M")

    async def trigger_snapshot_now() -> None:
        """/snap now：立即采集一轮（collect 幂等，含补漏桶/补算）+ 立即推送。"""
        await _snapshot_collect_round(snap_collector, markets)
        await trigger_snapshot_push()

    tasks = [
        asyncio.create_task(
            supervised(
                "rtds",
                lambda: rtds.run(event_queue, storage, reconcile_event, stop_event),
                stop_event,
            )
        ),
        asyncio.create_task(
            supervised(
                "data_api",
                lambda: data_api.run(event_queue, storage, reconcile_event, stop_event),
                stop_event,
            )
        ),
        asyncio.create_task(supervised("pipeline", pipeline.run, stop_event)),
        asyncio.create_task(heartbeat_writer(stop_event)),
        asyncio.create_task(health_watcher(health, rtds, storage, stop_event)),
        asyncio.create_task(
            supervised(
                "prune",
                lambda: prune_loop(storage, settings, stop_event),
                stop_event,
            )
        ),
    ]
    if markets:
        collect_interval = int(snapshot_cfg.get("collect_interval_seconds", 600))
        tasks.append(
            asyncio.create_task(
                supervised(
                    "snapshot_collect",
                    lambda: snapshot_collect_loop(
                        snap_collector,
                        markets,
                        stop_event,
                        collect_state,
                        interval_seconds=collect_interval,
                    ),
                    stop_event,
                )
            )
        )
    if snapshot_push_cfg.get("enabled", True) and snapshot_push_cfg.get("schedule"):
        tasks.append(
            asyncio.create_task(
                supervised(
                    "snapshot_push",
                    lambda: snapshot_push_loop(
                        storage, settings, trigger_snapshot_push, stop_event
                    ),
                    stop_event,
                )
            )
        )

    # 推送通道装配：notify.platforms 列表（telegram | ntfy，可多平台同时启用）。
    # 交易员告警队列经 run_sender 依次投递到全部通道；
    # telegram 指令轮询（/add /remove /vol 等）仅在启用 telegram 且 token 已配置时启动。
    trader_notifiers: list[Notifier] = []
    tg_api: TelegramApi | None = None
    auth: AuthManager | None = None
    if "telegram" in settings.notify_platforms and settings.bot_token:
        tg_api = TelegramApi(settings.bot_token)
        auth = AuthManager(settings.allowed_chat_ids, storage)
        await auth.load()
    trader_notifiers = build_notifiers(settings, storage, tg_api, auth, kind="trader")
    if trader_notifiers:
        tasks.append(
            asyncio.create_task(
                supervised(
                    "bot_sender",
                    lambda: run_sender(trader_notifiers, message_queue, stop_event),
                    stop_event,
                )
            )
        )
        logger.info(
            "交易员推送通道已启用: %s",
            [n.describe() for n in trader_notifiers],
        )
    else:
        logger.warning("推送通道均未启用，交易员告警不推送")
    if tg_api is not None and auth is not None:
        bot = BotRunner(tg_api, storage, auth)
        command_handler = CommandHandler(
            storage, health, digest_manager=pipeline.digest
        )
        tasks.append(
            asyncio.create_task(
                supervised(
                    "bot_updates",
                    lambda: bot.run_updates(command_handler, stop_event),
                    stop_event,
                )
            )
        )

    # 快照推送通道（独立白名单 / 独立 topic；与交易员通道隔离，但平台列表共用）
    snap_tg_api: TelegramApi | None = None
    if "telegram" in settings.notify_platforms and settings.snapshot_bot_token:
        snap_tg_api = TelegramApi(settings.snapshot_bot_token)
        snap_auth = AuthManager([], storage, state_key="snap_allowed_chats")
        await snap_auth.load()
        snap_runner = BotRunner(snap_tg_api, storage, snap_auth)
        snap_cmd = SnapshotCommandHandler(
            markets,
            snap_collector,
            storage,
            trigger_snapshot_now,
            snap_health,
            markets_path=str(settings.config_path.parent / "markets.toml"),
        )
        tasks.append(
            asyncio.create_task(
                supervised(
                    "snap_bot_updates",
                    lambda: snap_runner.run_updates(snap_cmd, stop_event),
                    stop_event,
                )
            )
        )
        logger.info("快照 Bot 已启用（独立白名单）")
    snap_notifiers = build_notifiers(
        settings, storage, snap_tg_api, snap_auth, kind="snapshot"
    )
    if snap_notifiers:
        logger.info(
            "快照推送通道已启用: %s",
            [n.describe() for n in snap_notifiers],
        )
    else:
        logger.warning("快照推送通道均未启用，快照仅采集不推送")

    loop = asyncio.get_running_loop()

    def _on_stop() -> None:
        logger.info("收到停止信号，准备退出")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_stop)

    await stop_event.wait()

    # 收尾：聚合组立即产出 → 短暂等待发送 → 取消全部
    await pipeline.shutdown()
    await asyncio.sleep(1)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await snap_collector.aclose()
    await storage.close()
    logger.info("服务已停止")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket 交易员监控")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
