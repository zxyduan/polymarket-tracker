"""服务入口：装配并监护全部异步任务。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv

from src.bot.auth import AuthManager
from src.bot.commands import CommandHandler
from src.bot.telegram import BotRunner, TelegramApi, run_sender
from src.core.models import StandardEvent
from src.core.pipeline import PipelineRunner
from src.core.renderer import AlertMessage
from src.notify import Notifier, build_notifiers
from src.settings import DailyReportConfig, Settings, resolve_daily_config
from src.snapshots.collector import (
    SnapshotCollector,
    _current_bucket_start,
)
from src.snapshots.commands import DailyCommandHandler, SnapshotCommandHandler
from src.snapshots.renderer import render_daily_volume_report, render_snapshot_summary
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
    """解析时间 HH:MM 列表 → [(hour, minute, label)]；24:00 归一为 00:00。

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
    """按配置的时间整点推送总览；已推过的整点不重推，错过的整点不补推。

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


# ── 每日成交份额汇总：定时循环（日报功能代号 daily_report）──────────────

# 发送失败后的同分钟重试等待（远小于 60s，保证 07:00 分钟内至少重试一次）
_DAILY_RETRY_WAIT_SECONDS = 5


async def daily_report_loop(
    storage: Storage,
    cfg: DailyReportConfig,
    trigger_daily_report: Callable[[], Awaitable[str]],
    stop_event: asyncio.Event,
) -> None:
    """每日单时刻推送日报；已推过当日不重推；错过不补发；同分钟内失败可重试。

    与 snapshot_push_loop 的有意差异：基线触发后无条件写 state="1"；
    本循环仅在终值（sent/degraded/selfcheck_failed/sent_dup）落 state，
    send_failed 不写 state，以便同一分钟内下一轮重试（错过时刻后自然放弃）。
    """
    local_tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
    while not stop_event.is_set():
        now_local = datetime.now(local_tz)
        date_key = now_local.strftime("%Y-%m-%d")
        state_key = f"daily_report:{date_key}"
        if (now_local.hour, now_local.minute) == (cfg.time_hour, cfg.time_minute):
            if await storage.get_state(state_key) is None:
                try:
                    result = await trigger_daily_report()
                except Exception as exc:  # noqa: BLE001 - 循环自身兜底，supervised 再兜底
                    logger.error("日报触发异常: %s", exc)
                    result = "send_failed"
                if result != "send_failed":
                    # 四种终值均落 state；send_failed 不写，等待同分钟下一轮重试
                    await storage.set_state(state_key, result)
                    logger.info("当日日报完成: %s = %s", date_key, result)
            # 终值已落 → 长等避开本分钟；send_failed 未落 state → 短等以便同分钟重试
            wait_seconds = (
                60 if await storage.get_state(state_key) is not None
                else _DAILY_RETRY_WAIT_SECONDS
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


# ── 日报分段助手（触发层职责：renderer 不感知段号，S9）───────────────────

# Telegram 单条 4096 字符、ntfy 公开服务器单条 4096 字节（UTF-8 中文 3 字节/字），
# 同一段要同时投递两渠道，取双上限同时守住。
_DAILY_MAX_CHARS = 4096
_DAILY_MAX_BYTES = 4096


def _daily_oversize(text: str) -> bool:
    """判断一段正文是否超出双通道任一长度上限。"""
    return (
        len(text) > _DAILY_MAX_CHARS
        or len(text.encode("utf-8")) > _DAILY_MAX_BYTES
    )


def _fit_daily_chunk(
    chunk: list[str], tail: list[str], title: str
) -> list[list[str]]:
    """把一组市场行拆到不超上限；仍超限（单行过长）则原样保留并交给调用方记日志。"""
    if not chunk:
        return [chunk]
    probe = "\n".join([title, *chunk, *tail])
    if not _daily_oversize(probe) or len(chunk) == 1:
        return [chunk]
    mid = max(1, len(chunk) // 2)
    return _fit_daily_chunk(chunk[:mid], [], title) + _fit_daily_chunk(
        chunk[mid:], tail, title
    )


def _paginate_daily(
    title: str,
    lines: list[str],
    footer: str,
    truncated_note: str,
    page_size: int,
) -> list[str]:
    """组装最终发送段列表（每项为完整段正文）。

    先按 page_size 市场数一段；再对超限段按行二分兜底（正常配置不触发）；
    首段用原标题，续段标题追加"（续 i/n）"；footer 与截断提示只附最后一段。
    """
    tail = [x for x in (footer, truncated_note) if x]
    chunks: list[list[str]] = []
    for i in range(0, len(lines), page_size):
        chunks.append(lines[i : i + page_size])
    if not chunks:
        chunks = [[]]
    fitted: list[list[str]] = []
    for ci, chunk in enumerate(chunks):
        is_last = ci == len(chunks) - 1
        fitted.extend(_fit_daily_chunk(chunk, tail if is_last else [], title))
    total = len(fitted)
    if total > len(chunks):
        logger.warning("日报分段触发超长二次切分: %d -> %d 段", len(chunks), total)
    segments: list[str] = []
    for si, chunk in enumerate(fitted, 1):
        head = title if si == 1 else f"{title}（续 {si}/{total}）"
        body = [head, *chunk]
        if si == total:
            body += tail
        text = "\n".join(body)
        if _daily_oversize(text):
            logger.warning("日报第 %d/%d 段仍超长度上限（单行过长），按原文发送", si, total)
        segments.append(text)
    return segments


async def trigger_daily_report(
    *,
    storage: Storage,
    collector: Any,
    markets: list[Any],
    cfg: DailyReportConfig,
    notifiers: list[Notifier],
    health: dict[str, Any],
) -> str:
    """生成并推送当日成交份额日报；返回结果码（成败以 notifications 落痕为准）。

    模块级函数（依赖显式注入，便于测试替换通道/采集器），由 amain 的
    _fire_daily_report 闭包绑定真实依赖后交给 daily_report_loop 调用。

    结果码全集——终值（写 state）：sent / degraded / selfcheck_failed / sent_dup；
    非终值（不写 state，允许同分钟重试）：send_failed。
    """
    import hashlib

    degraded = False
    failed_windows = 0

    # 步骤 1+2：市场清单（settings.markets 已过滤 enabled）与 cid 解析
    resolved_cids: dict[str, str] = {}
    for m in markets:
        try:
            cid = (m.condition_id or "").strip()
            if not cid:
                resolved = await collector.resolve_market(m.slug)
                if not resolved:
                    logger.warning("日报市场 cid 解析失败: slug=%s", m.slug)
                    continue
                cid = resolved[0]
            resolved_cids[m.slug] = cid.lower()
        except Exception as exc:  # noqa: BLE001 - 单市场失败不影响其余
            logger.warning("日报市场 cid 处理异常 slug=%s: %s", m.slug, exc)

    # 步骤 3：补算刚闭合桶（幂等；失败桶保持 -1 → 求和贡献 0）
    for cid in resolved_cids.values():
        try:
            await collector.backfill_prev_bucket(cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("日报 backfill 失败 cid=%s: %s", cid, exc)

    # 步骤 4：三窗口聚合（单窗口异常置降级并跳过；全部失败才走降级正文）
    R = _current_bucket_start()
    agg: dict[str, dict[int, dict[str, Any]]] = {}
    for d in cfg.windows_days:
        start = R - d * 86400
        try:
            rows_window = await storage.volume_by_window(start, R)
        except Exception as exc:  # noqa: BLE001 - 单窗口失败降级
            degraded = True
            failed_windows += 1
            logger.error("日报窗口聚合失败 d=%s: %s", d, exc)
            continue
        for row in rows_window:
            agg.setdefault(str(row["condition_id"]).lower(), {})[d] = row

    # 步骤 5：合并行（窗口内无行的市场三档置 0；显示名以 markets.toml 为准）
    rows: list[dict[str, Any]] = []
    for m in markets:
        cid = resolved_cids.get(m.slug)
        if not cid:
            continue
        bucket = agg.get(cid, {})
        row: dict[str, Any] = {
            "condition_id": cid,
            "slug_id": m.slug_id or m.slug,
            "slug": m.slug,
        }
        for d in cfg.windows_days:
            shares = float(bucket.get(d, {}).get("shares", 0.0) or 0.0)
            row[f"shares_{d}d"] = max(0.0, shares)
        rows.append(row)

    # 步骤 6：单调自检（定稿决策：仅在全部窗口聚合成功时执行）。
    # 部分窗口失败时该档按 0 计，与更长窗口的真实值比较会形成假逆序，把本可
    # 降级发送的日报误拒；此时跳过自检，缺失档的不可信由 degraded 标记承担。
    # 全部窗口成功时才对相邻档做单调性校验，任一逆序整体拒发（selfcheck_failed）。
    if failed_windows == 0:
        for row in rows:
            values = [row[f"shares_{d}d"] for d in cfg.windows_days]
            if any(a > b for a, b in pairwise(values)):
                logger.error(
                    "daily_report selfcheck failed cid=%s values=%s",
                    row["condition_id"], values,
                )
                health["daily_result"] = "selfcheck_failed"
                return "selfcheck_failed"
    else:
        logger.warning(
            "daily_report 跳过单调自检：%d/%d 个窗口聚合失败，缺失档按 0 计不可信",
            failed_windows,
            len(cfg.windows_days),
        )

    # 步骤 7：渲染（正常 / 空态 / 全窗口失败降级，统一落到三个变量）
    report = None
    if failed_windows != len(cfg.windows_days):
        report = render_daily_volume_report(
            rows,
            windows_days=cfg.windows_days,
            now_bucket=R,
            tz_offset_hours=cfg.timezone_offset_hours,
            top_n=cfg.top_n,
        )
    if report is not None:
        render_title = report.title
        render_lines = report.market_lines
        render_footer = report.footer
        truncated_note = report.truncated_note
    else:
        degraded = True
        local_tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
        r_local = datetime.fromtimestamp(R, tz=local_tz)
        render_title = (
            f"📈 每日成交份额汇总 · {r_local.month}月{r_local.day}日（截至今晨06:00）"
        )
        if failed_windows == len(cfg.windows_days):
            render_lines = ["今日数据生成失败（聚合查询异常，见日志）"]
        else:
            render_lines = ["暂无监控市场"]
        render_footer = ""
        truncated_note = ""

    # 步骤 8.1：组装分段（先组装完，再算指纹、再发送；renderer 不参与分段）
    segments = _paginate_daily(
        render_title, render_lines, render_footer, truncated_note, cfg.page_size
    )

    # 步骤 8.2：内容防重（dedup）
    body_hash = hashlib.sha1("\n".join(segments).encode("utf-8")).hexdigest()
    if cfg.dedup and await storage.get_state("daily_report:body_hash") == body_hash:
        logger.info("日报跳过：正文与上一条完全一致（去重）")
        health["daily_result"] = "sent_dup"
        return "sent_dup"

    # 步骤 8.3：段 × 通道逐个发送（内层各自捕获，单段单通道失败不中断其余）
    w0 = await storage.latest_notification_id()
    for seg_index, segment in enumerate(segments):
        for notifier in notifiers:
            try:
                await notifier.send(segment, event_keys=["daily_report"])
            except Exception as exc:  # noqa: BLE001
                # telegram 内部自行落 success/failed 且不抛；此处主要承接 ntfy 上抛
                logger.error(
                    "日报发送失败(%s) 段%d: %s",
                    notifier.describe(), seg_index + 1, exc,
                )
                await storage.record_notification(
                    notifier.describe(), segment, "daily_report", "failed"
                )
        if seg_index < len(segments) - 1:
            await asyncio.sleep(cfg.segment_delay_seconds)

    # 步骤 9：按归属水位判定成败（不能用 try/except 判定——telegram 吞异常）
    counts = await storage.count_notifications_since(w0, "daily_report")
    if counts["success"] > 0:
        result = "degraded" if degraded else "sent"
        if cfg.dedup:
            # 只有投递成功才记录去重指纹（对齐基线"成功才落指纹"语义）
            await storage.set_state("daily_report:body_hash", body_hash)
    else:
        result = "send_failed"
        if not notifiers:
            logger.warning("日报无可用通道，正文未推送: notifiers=0")
        elif counts["failed"] == 0:
            logger.warning("日报无任何归属落痕，判定失败: notifiers=%d", len(notifiers))

    health["daily_last_push"] = datetime.now(
        timezone(timedelta(hours=cfg.timezone_offset_hours))
    ).strftime("%Y-%m-%d %H:%M:%S")
    health["daily_result"] = result
    return result


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
    _p1_snap_uc = None  # P1 usecase 装配后替换

    # 日报通道与状态（延迟装配，模式对齐 snap_notifiers；闭包调用时才读取列表内容）
    daily_notifiers: list[Notifier] = []
    _p1_daily_uc = None  # P1 usecase 装配后替换
    daily_health: dict[str, Any] = {"daily_last_push": "-", "daily_result": "-"}
    daily_cfg = resolve_daily_config(settings)
    for _warn in daily_cfg.warnings:
        logger.warning("daily_report 配置警告: %s", _warn)
    for _err in daily_cfg.errors:
        logger.error("daily_report 配置致命错误: %s", _err)
    if not daily_cfg.enabled:
        logger.info("daily_report 未启用或配置存在致命错误，日报任务不注册")

    async def trigger_snapshot_push() -> None:
        """读取最近完整桶 → 渲染总览 → 推送到快照 Bot 白名单。"""
        import hashlib

        # P1 新骨架：开关打开时走 usecase
        if _p1_snap_uc is not None:
            await _p1_snap_uc.run()
            return

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

    async def _fire_daily_report() -> str:
        """amain 层薄封装：日报流程显式注入依赖，notifiers 调用时才取最新装配。"""
        if _p1_daily_uc is not None:
            return await _p1_daily_uc.run()
        return await trigger_daily_report(
            storage=storage,
            collector=snap_collector,
            markets=markets,
            cfg=daily_cfg,
            notifiers=daily_notifiers,
            health=daily_health,
        )

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
            **({
                "trigger_daily": _fire_daily_report,
                "daily_health": daily_health,
            } if daily_cfg.enabled else {}),
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

    # ── 日报通道装配（B1：token 选择在 amain 层，factory 不读 token）──
    if daily_cfg.enabled:
        daily_tg_api: TelegramApi | None = None
        daily_auth: AuthManager | None = None
        if "telegram" in settings.notify_platforms:
            independent_token = settings.daily_bot_token
            if independent_token:
                # 情形一：独立日报 Bot —— 三件套独立装配，白名单隔离，只暴露 /daily
                daily_tg_api = TelegramApi(independent_token)
                daily_auth = AuthManager([], storage, state_key="daily_allowed_chats")
                await daily_auth.load()
                daily_runner = BotRunner(daily_tg_api, storage, daily_auth)
                daily_cmd = DailyCommandHandler(
                    trigger_daily=_fire_daily_report,
                    daily_health=daily_health,
                )
                tasks.append(
                    asyncio.create_task(
                        supervised(
                            "daily_bot_updates",
                            lambda: daily_runner.run_updates(daily_cmd, stop_event),
                            stop_event,
                        )
                    )
                )
                logger.info("日报独立 Bot 已启用（独立白名单 daily_allowed_chats）")
            elif snap_tg_api is not None and snap_auth is not None:
                # 情形二（默认）：复用快照 Bot 的 api / 白名单 / snap_cmd（已带 /daily）
                daily_tg_api = snap_tg_api
                daily_auth = snap_auth
            else:
                logger.warning(
                    "日报 telegram 通道未配置 token（DAILY_BOT_TOKEN / "
                    "SNAPSHOT_BOT_TOKEN 均空），跳过"
                )
        daily_notifiers = build_notifiers(
            settings, storage, daily_tg_api, daily_auth, kind="daily"
        )
        if daily_notifiers:
            logger.info(
                "日报推送通道已启用: %s",
                [n.describe() for n in daily_notifiers],
            )
        else:
            logger.warning("日报推送通道均未启用，日报不会推送")
        if daily_notifiers:
            tasks.append(
                asyncio.create_task(
                    supervised(
                        "daily_report",
                        lambda: daily_report_loop(
                            storage, daily_cfg, _fire_daily_report, stop_event
                        ),
                        stop_event,
                    )
                )
            )

    loop = asyncio.get_running_loop()

    # ── P1 新骨架装配（双轨：默认不接管，env P1_SWITCH=1 时启用）──
    try:
        import os
        from framework.bootstrap import build_runtime
        from framework.adapters.legacy_collector import LegacyCollectorAdapter
        from framework.adapters.notifier_channel import NotifierChannel
        from framework.store.snapshot_store import SnapshotStore
        from framework.store.state_store import StateStore
        from framework.scheduler.guard import BodyHashGuard
        from framework.delivery.router import ChannelRouter
        from framework.render.paginator import MessagePaginator
        from framework.models.message import RenderedMessage
        from features.market_snapshot.usecase import (
            SnapshotPushConfig, SnapshotPushUsecase,
        )

        p1_switch = os.environ.get("P1_SWITCH", "1") == "1"
        p1_channels = [NotifierChannel(n) for n in snap_notifiers]

        from features.market_snapshot.renderer import LegacySnapshotRenderer
        from features.daily_report.renderer import LegacyDailyRenderer
        from features.daily_report.usecase import (
            DailyReportConfig, DailyReportUsecase,
        )

        class _Catalog:
            def slugs(self): return [m.slug for m in markets]

        if p1_switch:
            cursor = StateStore(storage)
            router = ChannelRouter(p1_channels, type("_P", (), {"targets": staticmethod(lambda: [])})())
            guard = BodyHashGuard(cursor)
            _p1_snap_uc = SnapshotPushUsecase(
                catalog=_Catalog(),
                collector=LegacyCollectorAdapter(snap_collector),
                query=SnapshotStore(storage),
                renderer=LegacySnapshotRenderer(
                    tz_offset_hours=int(snapshot_push_cfg.get("timezone_offset_hours", 8)),
                    bucket_seconds=int(snapshot_cfg.get("bucket_seconds", 7200)),
                ),
                router=router,
                body_hash=guard,
                config=SnapshotPushConfig(dedup=bool(snapshot_push_cfg.get("dedup", True)),
                                         now_bucket=_current_bucket_start()),
                health=snap_health,
            )
            logger.info("P1 快照 usecase 已接管推送")

            daily_router = ChannelRouter(
                [NotifierChannel(n) for n in daily_notifiers],
                type("_P", (), {"targets": staticmethod(lambda: [])})(),
            )
            _p1_daily_uc = DailyReportUsecase(
                catalog=_Catalog(),
                query=SnapshotStore(storage),
                renderer=LegacyDailyRenderer(),
                router=daily_router,
                paginator=MessagePaginator(),
                body_hash=guard,
                config=DailyReportConfig(
                    windows_days=list(daily_cfg.windows_days),
                    timezone_offset_hours=daily_cfg.timezone_offset_hours,
                    top_n=daily_cfg.top_n,
                    page_size=daily_cfg.page_size,
                    now_bucket=_current_bucket_start(),
                ),
                health=daily_health,
            )
            logger.info("P1 日报 usecase 已接管")
    except Exception:  # noqa: BLE001 - 装配失败不影响旧链路
        logger.exception("P1 bootstrap 装配失败，回退旧链路")

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
    parser = argparse.ArgumentParser(description="market 交易员监控")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
