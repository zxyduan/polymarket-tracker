"""手动单次跑一轮 market_screen 采集落库（不经定时 loop 与当日防重 state）。

用法（仓库根目录、.venv 激活）：
    python scripts/run_screen_once.py
    python scripts/run_screen_once.py --config config/config.yaml

说明：
- 只跑市场筛选这一轮，不启动 RTDS/DataAPI/推送等常驻循环；
- 绕过当日 `market_screen:YYYY-MM-DD` 防重 state；当日分区先删后插，重复跑覆盖当天；
- 跑完用只读脚本 scripts/query_ms.py 查看结果。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 直接 `python scripts/run_screen_once.py` 运行时，把仓库根加入模块搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.sources.market_lister import GammaMarketLister
from framework.store.market_screen_store import MarketScreenStore
from features.market_screen.usecase import MarketScreenUsecase
from src.settings import Settings, resolve_market_screen_config
from src.storage import Storage


async def main() -> None:
    parser = argparse.ArgumentParser(description="单次运行 market_screen 采集轮")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("run_screen_once")

    settings = Settings.load(args.config)
    cfg = resolve_market_screen_config(settings)
    for warn in cfg.warnings:
        logger.warning("market_screen 配置警告: %s", warn)
    for err in cfg.errors:
        logger.error("market_screen 配置错误: %s", err)
    if not cfg.enabled:
        logger.error("market_screen 未启用（enabled=false 或配置致命错误），退出")
        return

    endpoints = settings.section("endpoints")
    storage = Storage("data/monitor.db")
    await storage.init()
    try:
        lister = GammaMarketLister(
            base_url=str(
                endpoints.get("gamma_api", "https://gamma-api.polymarket.com")
            ),
            timeout_seconds=cfg.request_timeout_seconds,
            max_retries=cfg.retry_attempts,
            retry_backoff_seconds=cfg.retry_backoff_seconds,
            page_interval_seconds=cfg.page_delay_seconds,
        )
        store = MarketScreenStore(storage, config=cfg)
        usecase = MarketScreenUsecase(lister, store, config=cfg)
        result = await usecase.run()
        logger.info("market_screen 单次运行结果: %s", result)
        await lister.aclose()
    finally:
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
