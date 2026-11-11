"""应用装配：build_app() 集中创建所有客户端、通道、任务并返回运行句柄。

从 src/main.py 搬出；main.py 只调用 build_app() 并等待停止信号。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import replace
from typing import Any

from dotenv import load_dotenv

from src.bot.auth import AuthManager
from src.bot.commands import CommandHandler
from src.bot.telegram import BotRunner, TelegramApi
from framework.models.event import StandardEvent
from src.core.pipeline import PipelineRunner
from src.core.renderer import AlertMessage
from src.notify import Notifier, build_notifiers
from src.settings import MarketEntry, Settings, resolve_daily_config, resolve_market_screen_config
from src.snapshots.collector import SnapshotCollector
from src.snapshots.commands import DailyCommandHandler, SnapshotCommandHandler
from src.sources.data_api_client import DataApiClient
from src.sources.gamma_client import GammaClient
from src.sources.rtds_client import RtdsClient
from src.storage import Storage
from src.utils.logging import setup_logging
from src.utils.retry import backoff_delay

from framework.runtime.loops import (
    daily_report_loop,
    funnel_watch_loop,
    heartbeat_writer,
    health_watcher,
    market_screen_loop,
    prune_loop,
    snapshot_collect_loop,
    snapshot_push_loop,
    supervised,
    _snapshot_collect_round,
)
from framework.sources.market_lister import GammaMarketLister
from framework.store.market_screen_store import MarketScreenStore
from features.market_screen.usecase import MarketScreenUsecase

logger = logging.getLogger("app")


class AppHandle:
    """运行句柄：build_app() 返回，main.py 用它等待停止并收尾。"""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []
        self.stop_event: asyncio.Event = asyncio.Event()
        self.storage: Storage | None = None
        self.snap_collector: SnapshotCollector | None = None
        self.pipeline: PipelineRunner | None = None
        self.ms_lister: GammaMarketLister | None = None


async def build_app(config_path: str) -> AppHandle:
    load_dotenv()
    settings = Settings.load(config_path)
    setup_logging(
        settings.log_level,
        log_file=settings.get("logging.file"),
        max_mb=int(settings.get("logging.max_mb", 10)),
        backups=int(settings.get("logging.backups", 5)),
    )
    logger.info("服务启动")

    h = AppHandle()

    # 存储
    storage = Storage("data/monitor.db")
    await storage.init()
    h.storage = storage
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
    stop_event = h.stop_event

    # 冷启动基线
    wallets = await storage.list_wallets(active_only=True)
    cold_attempt = 0
    while not stop_event.is_set():
        try:
            await data_api.cold_start(wallets, storage)
            break
        except Exception as exc:  # noqa: BLE001
            cold_attempt += 1
            delay = backoff_delay(cold_attempt, 2.0, 60.0)
            logger.warning("冷启动基线建立失败: %s，%.0fs 后重试", exc, delay)
            await asyncio.sleep(delay)

    pipeline = PipelineRunner(event_queue, message_queue, storage, gamma, settings)
    h.pipeline = pipeline
    health: dict[str, Any] = {"rtds_connected": False, "l2_last_run": "-"}
    health["pipeline_funnel"] = pipeline.funnel

    # ── 市场快照 ──
    snapshot_push_cfg = settings.snapshot_push
    snapshot_cfg = settings.snapshot
    markets = settings.markets
    if markets:
        logger.info("快照市场 %d 个: %s", len(markets), [m.slug_id for m in markets])
    else:
        logger.info("快照市场为空，仅注册推送循环")
    snap_collector = SnapshotCollector(
        storage,
        gamma_host=str(endpoints.get("gamma_api", "https://gamma-api.polymarket.com")),
        clob_host=str(endpoints.get("clob_api", "https://clob.polymarket.com")),
        timeout=float(snapshot_cfg.get("request_timeout_seconds", 15)),
        price_fidelity_minutes=int(snapshot_cfg.get("price_fidelity_minutes", 60)),
    )
    h.snap_collector = snap_collector

    # 方案 B：日报需要 condition_id；配置未填时启动按 slug 自动解析，
    # 快照采集循环仍自行解析，这里只补充日报 Catalog 使用的只读副本。
    resolved_markets: list[MarketEntry] = []
    unresolved: list[str] = []
    for m in markets:
        cid = (m.condition_id or "").strip()
        if not cid:
            try:
                resolved = await snap_collector.resolve_market(m.slug)
            except Exception as exc:  # noqa: BLE001 - 单市场失败不阻断启动
                logger.warning("市场 cid 自动解析失败 %s: %s", m.slug_id, exc)
                resolved = None
            cid = resolved[0] if resolved else ""
        if cid:
            resolved_markets.append(replace(m, condition_id=cid))
        else:
            unresolved.append(m.slug_id)
            resolved_markets.append(m)
    if unresolved:
        logger.warning("以下市场未解析到 condition_id，日报将跳过: %s", ", ".join(unresolved))

    snap_health: dict[str, Any] = {"snap_last_push": "-"}
    collect_state: dict[str, float] = {"round_done_ts": 0.0}
    snap_notifiers: list[Notifier] = []

    # P1 usecase 引用容器（装配后填充）
    p1: dict[str, Any] = {}

    # 灰度开关：交易推送新路径是否启用（默认开，出问题设 false 回退旧路径）
    p1_trader_enabled = bool(settings.get("p1.trader_enabled", True))
    if p1_trader_enabled:
        logger.info("交易推送新路径（P1 TraderAlertUsecase）已启用")
    else:
        logger.warning("交易推送新路径已关闭，走旧 Notifier 直连路径")

    async def _trader_consumer(queue, stop):
        while not stop.is_set():
            alert = await queue.get()
            try:
                if p1_trader_enabled and "trader" in p1:
                    result = await p1["trader"].handle(alert)
                    title = getattr(alert, "title", "") or "交易告警"
                    if result.channel_failures:
                        logger.error(
                            "推送失败：%s（%d 个通道失败）",
                            title, len(result.channel_failures),
                        )
                    else:
                        logger.info("推送成功：%s", title)
                else:
                    for n in trader_notifiers:
                        try:
                            await n.send(alert.html, buttons=alert.buttons,
                                         event_keys=alert.event_keys)
                            logger.info(
                                "推送成功（旧路径）：%s", n.describe()
                            )
                        except Exception as exc:
                            logger.error("推送失败(%s): %s", n.describe(), exc)
            finally:
                queue.task_done()

    # ── 日报 ──
    daily_notifiers: list[Notifier] = []
    daily_health: dict[str, Any] = {"daily_last_push": "-", "daily_result": "-"}
    daily_cfg = resolve_daily_config(settings)
    for _warn in daily_cfg.warnings:
        logger.warning("daily_report 配置警告: %s", _warn)
    for _err in daily_cfg.errors:
        logger.error("daily_report 配置致命错误: %s", _err)
    if not daily_cfg.enabled:
        logger.info("daily_report 未启用或配置存在致命错误，日报任务不注册")

    async def trigger_snapshot_push() -> str:
        if "snap" in p1:
            return await p1["snap"]()
        logger.error("快照推送 usecase 未装配")
        return "send_failed"

    async def trigger_snapshot_now() -> None:
        await _snapshot_collect_round(snap_collector, markets)
        await trigger_snapshot_push()

    async def _fire_daily_report() -> str:
        if "daily" in p1:
            return await p1["daily"]()
        logger.error("日报 usecase 未装配，返回 send_failed")
        return "send_failed"

    tasks = [
        asyncio.create_task(supervised(
            "rtds", lambda: rtds.run(event_queue, storage, reconcile_event, stop_event), stop_event)),
        asyncio.create_task(supervised(
            "data_api", lambda: data_api.run(event_queue, storage, reconcile_event, stop_event), stop_event)),
        asyncio.create_task(supervised("pipeline", pipeline.run, stop_event)),
        asyncio.create_task(heartbeat_writer(stop_event)),
        asyncio.create_task(health_watcher(health, rtds, storage, stop_event)),
        asyncio.create_task(supervised(
            "prune", lambda: prune_loop(storage, settings, stop_event), stop_event)),
    ]
    if markets:
        collect_interval = int(snapshot_cfg.get("collect_interval_seconds", 600))
        tasks.append(asyncio.create_task(supervised(
            "snapshot_collect",
            lambda: snapshot_collect_loop(snap_collector, markets, stop_event,
                                          collect_state, interval_seconds=collect_interval),
            stop_event)))
    if snapshot_push_cfg.get("enabled", True) and snapshot_push_cfg.get("schedule"):
        tasks.append(asyncio.create_task(supervised(
            "snapshot_push",
            lambda: snapshot_push_loop(storage, settings, trigger_snapshot_push, stop_event),
            stop_event)))

    # ── 推送通道装配 ──
    trader_notifiers: list[Notifier] = []
    tg_api: TelegramApi | None = None
    auth: AuthManager | None = None
    if "telegram" in settings.notify_platforms and settings.bot_token:
        tg_api = TelegramApi(settings.bot_token)
        auth = AuthManager(settings.allowed_chat_ids, storage)
        await auth.load()
    trader_notifiers = build_notifiers(settings, storage, tg_api, auth, kind="trader")
    if trader_notifiers:
        tasks.append(asyncio.create_task(supervised(
            "bot_sender", lambda: _trader_consumer(message_queue, stop_event), stop_event)))
        logger.info("交易员推送通道已启用: %s", [n.describe() for n in trader_notifiers])
    else:
        logger.warning("推送通道均未启用，交易员告警不推送")
    if tg_api is not None and auth is not None:
        bot = BotRunner(tg_api, storage, auth)
        command_handler = CommandHandler(storage, health, digest_manager=pipeline.digest)
        tasks.append(asyncio.create_task(supervised(
            "bot_updates", lambda: bot.run_updates(command_handler, stop_event), stop_event)))

    # ── 快照推送通道 ──
    snap_tg_api: TelegramApi | None = None
    snap_auth: AuthManager | None = None
    if "telegram" in settings.notify_platforms and settings.snapshot_bot_token:
        snap_tg_api = TelegramApi(settings.snapshot_bot_token)
        snap_auth = AuthManager([], storage, state_key="snap_allowed_chats")
        await snap_auth.load()
        snap_runner = BotRunner(snap_tg_api, storage, snap_auth)
        snap_cmd = SnapshotCommandHandler(
            markets, snap_collector, storage, trigger_snapshot_now, snap_health,
            markets_path=str(settings.config_path.parent / "markets.toml"),
            **({"trigger_daily": _fire_daily_report, "daily_health": daily_health}
               if daily_cfg.enabled else {}),
        )
        tasks.append(asyncio.create_task(supervised(
            "snap_bot_updates", lambda: snap_runner.run_updates(snap_cmd, stop_event), stop_event)))
        logger.info("快照 Bot 已启用（独立白名单）")
    snap_notifiers = build_notifiers(settings, storage, snap_tg_api, snap_auth, kind="snapshot")
    if snap_notifiers:
        logger.info("快照推送通道已启用: %s", [n.describe() for n in snap_notifiers])
    else:
        logger.warning("快照推送通道均未启用，快照仅采集不推送")

    # ── 日报通道 ──
    if daily_cfg.enabled:
        daily_tg_api: TelegramApi | None = None
        daily_auth: AuthManager | None = None
        if "telegram" in settings.notify_platforms:
            independent_token = settings.daily_bot_token
            if independent_token:
                daily_tg_api = TelegramApi(independent_token)
                daily_auth = AuthManager([], storage, state_key="daily_allowed_chats")
                await daily_auth.load()
                daily_runner = BotRunner(daily_tg_api, storage, daily_auth)
                daily_cmd = DailyCommandHandler(
                    trigger_daily=_fire_daily_report, daily_health=daily_health)
                tasks.append(asyncio.create_task(supervised(
                    "daily_bot_updates",
                    lambda: daily_runner.run_updates(daily_cmd, stop_event), stop_event)))
                logger.info("日报独立 Bot 已启用")
            elif snap_tg_api is not None and snap_auth is not None:
                daily_tg_api = snap_tg_api
                daily_auth = snap_auth
            else:
                logger.warning("日报 telegram 通道未配置 token，跳过")
        daily_notifiers = build_notifiers(
            settings, storage, daily_tg_api, daily_auth, kind="daily")
        if daily_notifiers:
            logger.info("日报推送通道已启用: %s", [n.describe() for n in daily_notifiers])
        else:
            logger.warning("日报推送通道均未启用，日报不会推送")
        if daily_notifiers:
            tasks.append(asyncio.create_task(supervised(
                "daily_report",
                lambda: daily_report_loop(storage, daily_cfg, _fire_daily_report, stop_event),
                stop_event)))

    # ── 每日市场筛选（直连注册，不经 feature bootstrap）──
    ms_cfg = resolve_market_screen_config(settings)
    for _warn in ms_cfg.warnings:
        logger.warning("market_screen 配置警告: %s", _warn)
    for _err in ms_cfg.errors:
        logger.error("market_screen 配置致命错误: %s", _err)
    if ms_cfg.enabled:
        ms_lister = GammaMarketLister(
            base_url=str(endpoints.get("gamma_api", "https://gamma-api.polymarket.com")),
            timeout_seconds=ms_cfg.request_timeout_seconds,
            max_retries=ms_cfg.retry_attempts,
            retry_backoff_seconds=ms_cfg.retry_backoff_seconds,
            page_interval_seconds=ms_cfg.page_delay_seconds,
        )
        ms_store = MarketScreenStore(storage, config=ms_cfg)
        ms_usecase = MarketScreenUsecase(ms_lister, ms_store, config=ms_cfg)
        h.ms_lister = ms_lister
        tasks.append(asyncio.create_task(supervised(
            "market_screen",
            lambda: market_screen_loop(storage, ms_cfg, ms_usecase.run, stop_event),
            stop_event)))
        logger.info(
            "market_screen 任务已注册（schedule=%s, preset=%s, top_n=%d）",
            ms_cfg.schedule, ms_cfg.main_preset, ms_cfg.top_n,
        )
    else:
        logger.info("market_screen 未启用或配置存在致命错误，任务不注册")

    # ── feature bootstrap（必须成功，失败直接 crash）──
    from framework.bootstrap import build_runtime
    from features.market_snapshot.renderer import SnapshotSummaryRenderer
    from features.daily_report.renderer import DailyReportRenderer
    from features.trader.renderer import LegacyTraderRenderer
    from features.market_snapshot.usecase import SnapshotPushConfig
    from features.daily_report.usecase import DailyReportConfig

    class _Catalog:
        def slugs(self): return [m.slug for m in resolved_markets]

        def markets(self): return list(resolved_markets)

    snap_max_stale = int(snapshot_push_cfg.get("max_stale_buckets", 24))
    logger.info("快照陈旧守卫阈值=%d 桶（0=不启用）", snap_max_stale)
    _rt, _trader_uc = build_runtime(
        storage=storage, collector=snap_collector, catalog=_Catalog(),
        snap_notifiers=snap_notifiers, daily_notifiers=daily_notifiers,
        trader_notifiers=trader_notifiers,
        snap_renderer=SnapshotSummaryRenderer(
            tz_offset_hours=int(snapshot_push_cfg.get("timezone_offset_hours", 8)),
            bucket_seconds=int(snapshot_cfg.get("bucket_seconds", 7200)),
        ),
        daily_renderer=DailyReportRenderer(),
        trader_renderer=LegacyTraderRenderer(),
        snap_cfg=SnapshotPushConfig(
            dedup=bool(snapshot_push_cfg.get("dedup", True)),
            max_stale_buckets=snap_max_stale,
            now_bucket=0,
            bucket_seconds=int(snapshot_cfg.get("bucket_seconds", 7200)),
        ),
        daily_cfg=DailyReportConfig(
            windows_days=list(daily_cfg.windows_days),
            timezone_offset_hours=daily_cfg.timezone_offset_hours,
            top_n=daily_cfg.top_n, page_size=daily_cfg.page_size,
            now_bucket=0,
            bucket_seconds=int(daily_cfg.bucket_seconds),
        ),
        snap_health=snap_health, daily_health=daily_health,
    )
    p1["snap"] = next(m.run for m in _rt.modules if m.name == "market_snapshot")
    p1["daily"] = next(m.run for m in _rt.modules if m.name == "daily_report")
    p1["trader"] = _trader_uc
    health["trader_funnel"] = _trader_uc.funnel
    logger.info("三个 feature 已接管")
    logger.info("交易漏斗基线 pipeline=%s trader=%s", pipeline.funnel, _trader_uc.funnel)
    tasks.append(asyncio.create_task(supervised(
        "funnel_watch",
        lambda: funnel_watch_loop(pipeline, _trader_uc, stop_event),
        stop_event)))

    # 信号处理
    loop = asyncio.get_running_loop()
    def _on_stop() -> None:
        logger.info("收到停止信号，准备退出")
        stop_event.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_stop)

    h.tasks = tasks
    return h


async def shutdown_app(h: AppHandle) -> None:
    """收尾：等待发送完成 → 取消任务 → 关连接。"""
    if h.pipeline:
        await h.pipeline.shutdown()
    await asyncio.sleep(1)
    for task in h.tasks:
        task.cancel()
    await asyncio.gather(*h.tasks, return_exceptions=True)
    if h.snap_collector:
        await h.snap_collector.aclose()
    if h.ms_lister:
        await h.ms_lister.aclose()
    if h.storage:
        await h.storage.close()
    logger.info("服务已停止")
