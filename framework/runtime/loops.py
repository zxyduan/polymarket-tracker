"""运行时循环：心跳/健康/清理/快照采集推送/日报调度/任务监护。

从 src/main.py 搬出，main.py 只留入口装配。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.settings import DailyReportConfig, MarketScreenConfig, Settings
from src.snapshots.collector import SnapshotCollector
from src.storage import Storage
from src.utils.retry import backoff_delay

logger = logging.getLogger("loops")


# ── 基础设施循环 ─────────────────────────────────────────────────────────


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
    rtds: Any,
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


async def funnel_watch_loop(
    pipeline: Any,
    trader: Any,
    stop_event: asyncio.Event,
    interval_seconds: int = 300,
) -> None:
    """周期输出交易漏斗；ntfy-only 部署下 Telegram /status 不可用。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            logger.info(
                "交易漏斗 pipeline=%s trader=%s",
                pipeline.funnel,
                trader.funnel,
            )


async def prune_loop(storage: Storage, settings: Settings, stop_event: asyncio.Event) -> None:
    """每日清理：过期事件、过期推送记录、过期快照、过期筛选候选，并 VACUUM。"""
    events_days = int(settings.get("events.retention_days", 180))
    notifications_days = int(settings.get("events.notifications_retention_days", 30))
    snapshot_days = int(settings.snapshot.get("retention_days", 90))
    screen_days = int(settings.market_screen.get("retention_days", 30))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=86400)
        except asyncio.TimeoutError:
            await storage.prune_events(events_days)
            await storage.prune_notifications(notifications_days)
            await storage.prune_snapshots(snapshot_days)
            await storage.prune_screen(screen_days)
            await storage.vacuum()
            logger.info(
                "数据清理完成（事件 %d 天 / 推送记录 %d 天 / 快照 %d 天 / 筛选 %d 天 / 已 VACUUM）",
                events_days, notifications_days, snapshot_days, screen_days,
            )


# ── 市场快照采集与推送 ──────────────────────────────────────────────────


async def snapshot_collect_loop(
    collector: SnapshotCollector,
    markets: list[Any],
    stop_event: asyncio.Event,
    collect_state: dict[str, float] | None = None,
    interval_seconds: int = 600,
) -> None:
    """固定间隔采集（默认 10 分钟一轮）；启动先跑一轮。"""
    state = collect_state if collect_state is not None else {}
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
                    m.slug_id, bool(cid), bool(yes_token),
                )
                continue
            await collector.collect(
                condition_id=cid, yes_token_id=yes_token,
                slug=m.slug, slug_id=m.slug_id,
                fv_initial=m.fv_initial, fv_end_date=m.fv_end_date,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("快照采集失败 %s: %s", m.slug_id, exc)


def _parse_snapshot_schedule(schedule: list[str]) -> list[tuple[int, int, str]]:
    """解析时间 HH:MM 列表 → [(hour, minute, label)]；24:00 归一为 00:00。"""
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
    """按配置的时间整点推送总览；已推过的整点不重推，错过的整点不补推。"""
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
            skip_key = f"snap_skip:{date_key}:{label}"
            if await storage.get_state(state_key) or await storage.get_state(skip_key):
                continue
            try:
                result = await trigger_push()
            except Exception as exc:  # noqa: BLE001
                logger.error("整点推送失败 %s: %s", label, exc)
                pushed_any = True
                continue
            if result == "sent":
                await storage.set_state(state_key, "1")
                logger.info("整点快照已推送: %s %s", date_key, label)
            elif result == "dedup_skip":
                await storage.set_state(skip_key, "1")
                logger.info("快照正文未变化，去重抑制: %s %s", date_key, label)
            elif result in ("empty", "stale_skip", "render_none"):
                logger.warning(
                    "快照未发送 result=%s: %s %s", result, date_key, label
                )
            else:
                logger.error("快照推送失败 result=%s: %s %s", result, date_key, label)
            pushed_any = True
        if pushed_any:
            await asyncio.sleep(60)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


# ── 日报调度 ─────────────────────────────────────────────────────────────


_DAILY_RETRY_WAIT_SECONDS = 5


async def daily_report_loop(
    storage: Storage,
    cfg: DailyReportConfig,
    trigger_daily_report: Callable[[], Awaitable[str]],
    stop_event: asyncio.Event,
) -> None:
    """每日单时刻推送日报；终值落 state，send_failed 同分钟重试。"""
    local_tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
    while not stop_event.is_set():
        now_local = datetime.now(local_tz)
        date_key = now_local.strftime("%Y-%m-%d")
        state_key = f"daily_report:{date_key}"
        if (now_local.hour, now_local.minute) == (cfg.time_hour, cfg.time_minute):
            if await storage.get_state(state_key) is None:
                try:
                    result = await trigger_daily_report()
                except Exception as exc:  # noqa: BLE001
                    logger.error("日报触发异常: %s", exc)
                    result = "send_failed"
                if result != "send_failed":
                    await storage.set_state(state_key, result)
                    logger.info("当日日报完成: %s = %s", date_key, result)
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


# ── 市场筛选调度 ──────────────────────────────────────────────────────────


async def market_screen_loop(
    storage: Storage,
    cfg: MarketScreenConfig,
    trigger_screen: Callable[[], Awaitable[str]],
    stop_event: asyncio.Event,
) -> None:
    """每日单时刻跑市场筛选；done/empty 落 state 防重，failed 按 retry 秒重试。"""
    local_tz = timezone(timedelta(hours=cfg.timezone_offset_hours))
    while not stop_event.is_set():
        now_local = datetime.now(local_tz)
        date_key = now_local.strftime("%Y-%m-%d")
        state_key = f"market_screen:{date_key}"
        if (now_local.hour, now_local.minute) == (cfg.schedule_hour, cfg.schedule_minute):
            if await storage.get_state(state_key) is None:
                try:
                    result = await trigger_screen()
                except Exception as exc:  # noqa: BLE001
                    logger.error("市场筛选触发异常: %s", exc)
                    result = "failed"
                if result != "failed":
                    await storage.set_state(state_key, result)
                    logger.info("当日市场筛选完成: %s = %s", date_key, result)
            wait_seconds = (
                60 if await storage.get_state(state_key) is not None else cfg.retry_seconds
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


# ── 任务监护 ────────────────────────────────────────────────────────────


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
            except Exception as exc:  # noqa: BLE001
                logger.warning("任务 %s 异常退出: %s", name, exc)
            attempt += 1
            delay = backoff_delay(attempt, 1.0, 30.0)
            logger.info("任务 %s %.1fs 后重启", name, delay)
            await asyncio.sleep(delay)

    return wrapper()
