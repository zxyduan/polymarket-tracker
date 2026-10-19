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
from src.bot.telegram import BotRunner, TelegramApi
from src.core.models import StandardEvent
from src.core.pipeline import PipelineRunner
from src.core.renderer import AlertMessage
from src.settings import Settings
from src.snapshots.collector import (
    BUCKET_SIZE_SEC,
    SnapshotCollector,
    _current_bucket_start,
)
from src.snapshots.commands import SnapshotCommandHandler
from src.snapshots.renderer import render_snapshot_summary
from src.snapshots.volume_tracker import VolumeTracker
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
) -> None:
    """每 2 小时一轮采集（对齐 UTC 桶边界）；启动先跑一轮（含 slug 解析）。

    collect_state 记录最近一轮采集完成的时间戳，供整点推送等待
    （边界轮会补算上一闭合桶的成交量增量，推送必须等它完成才读到最新值）。
    """
    state = collect_state if collect_state is not None else {}
    await _snapshot_collect_round(collector, markets)
    state["round_done_ts"] = float(time.time())
    while not stop_event.is_set():
        now = int(time.time())
        delay = BUCKET_SIZE_SEC - (now % BUCKET_SIZE_SEC)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay + 2)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        await _snapshot_collect_round(collector, markets)
        state["round_done_ts"] = float(time.time())


async def _snapshot_collect_round(
    collector: SnapshotCollector, markets: list[Any]
) -> None:
    """采集一轮全部市场；单市场失败跳过不影响整轮。"""
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
    """解析北京时间 HH:MM 列表 → [(hour, minute, label)]；24:00 归一为 00:00。"""
    out: list[tuple[int, int, str]] = []
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
            out.append((0, 0, "24:00"))
        else:
            out.append((hour, minute, text))
    return out


async def snapshot_push_loop(
    storage: Storage,
    settings: Settings,
    trigger_push: Any,
    stop_event: asyncio.Event,
) -> None:
    """北京时间 7 整点推送总览；已推过的整点不重推，错过的整点不补推。"""
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

    # 快照成交量累加器：RTDS 全量成交按桶记账，供快照闭合桶补算精确增量
    volume_tracker = VolumeTracker()

    # 客户端
    rtds = RtdsClient(
        str(endpoints.get("rtds")),
        heartbeat_seconds=int(polling.get("rtds_heartbeat_seconds", 15)),
        reconnect_min_seconds=float(polling.get("reconnect_min_seconds", 1)),
        reconnect_max_seconds=float(polling.get("reconnect_max_seconds", 30)),
        volume_tracker=volume_tracker,
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
        volume_tracker=volume_tracker,
    )
    snap_health: dict[str, Any] = {"snap_last_push": "-"}
    snap_auth: AuthManager | None = None
    snap_runner: BotRunner | None = None
    # 快照采集完成状态：round_done_ts = 最近一轮采集完成时刻（供整点推送等待）
    collect_state: dict[str, float] = {"round_done_ts": 0.0}

    async def trigger_snapshot_push() -> None:
        """读取最近完整桶 → 渲染总览 → 推送到快照 Bot 白名单。"""
        now_bucket = _current_bucket_start()
        # 等待本桶边界的采集轮完成（该轮负责补算上一闭合桶增量），
        # 最多 120s；超时降级用旧值推送，不阻塞整点节奏。
        deadline = now_bucket + 120
        while (
            time.time() < deadline
            and collect_state["round_done_ts"] < now_bucket - 1
        ):
            await asyncio.sleep(2)
        rows = await storage.latest_complete_buckets(now_bucket)
        if not rows:
            logger.info("快照推送跳过：暂无完整桶数据")
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
        alert = render_snapshot_summary(
            rows,
            tz_offset_hours=int(snapshot_push_cfg.get("timezone_offset_hours", 8)),
            bucket_seconds=int(snapshot_cfg.get("bucket_seconds", 7200)),
        )
        if alert is None:
            return
        if snap_auth is not None and snap_runner is not None:
            for chat_id in snap_auth.chats():
                try:
                    await snap_runner.api.send_message(chat_id, alert.html)
                except Exception as exc:  # noqa: BLE001 - 单 chat 失败不影响
                    logger.error("快照推送 %s 失败: %s", chat_id, exc)
        else:
            logger.warning("SNAPSHOT_BOT_TOKEN 未配置，快照总览未推送（仅采集落库）")
        snap_health["snap_last_push"] = datetime.now(
            timezone(timedelta(hours=8))
        ).strftime("%m-%d %H:%M")

    async def trigger_snapshot_now() -> None:
        """/snap now：采集一轮 + 立即推送。"""
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
        asyncio.create_task(prune_loop(storage, settings, stop_event)),
    ]
    if markets:
        tasks.append(
            asyncio.create_task(
                supervised(
                    "snapshot_collect",
                    lambda: snapshot_collect_loop(
                        snap_collector, markets, stop_event, collect_state
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

    # Bot（Token 缺失时仅采集，不阻断）
    if settings.bot_token:
        tg_api = TelegramApi(settings.bot_token)
        auth = AuthManager(settings.allowed_chat_ids, storage)
        await auth.load()
        bot = BotRunner(tg_api, storage, auth)
        command_handler = CommandHandler(storage, health)
        tasks.append(
            asyncio.create_task(
                supervised(
                    "bot_sender",
                    lambda: bot.run_sender(message_queue, stop_event),
                    stop_event,
                )
            )
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
    else:
        logger.warning("BOT_TOKEN 未配置，仅采集不推送；配置后重启即可启用")

    # 快照 Bot（独立 token / 独立白名单 / 独立指令，与交易员 Bot 隔离）
    if settings.snapshot_bot_token:
        snap_api = TelegramApi(settings.snapshot_bot_token)
        snap_auth = AuthManager([], storage, state_key="snap_allowed_chats")
        await snap_auth.load()
        snap_runner = BotRunner(snap_api, storage, snap_auth)
        snap_cmd = SnapshotCommandHandler(
            markets,
            snap_collector,
            storage,
            trigger_snapshot_now,
            snap_health,
            volume_tracker=volume_tracker,
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
    else:
        logger.warning("SNAPSHOT_BOT_TOKEN 未配置，快照仅采集不推送")

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
