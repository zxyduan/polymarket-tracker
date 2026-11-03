"""服务入口：parse args → build_app → wait stop → shutdown。

装配逻辑在 framework/app.py，运行时循环在 framework/runtime/loops.py。
本文件仅做命令行入口和必要的 re-export（兼容旧测试 import）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging

# Re-export：测试仍从 src.main import 这些符号（避免大规模改测试 import）
from framework.runtime.loops import (  # noqa: F401
    _parse_snapshot_schedule,
    daily_report_loop,
)

logger = logging.getLogger("main")


async def amain(config_path: str) -> None:
    from framework.app import build_app, shutdown_app

    h = await build_app(config_path)
    await h.stop_event.wait()
    await shutdown_app(h)


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
