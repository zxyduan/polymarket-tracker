"""结构化日志配置：stdout + 大小轮转文件。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志，便于采集与检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def _quiet_noisy_loggers() -> None:
    """降噪：第三方库的请求级日志只在出错时打印，避免日志爆炸。"""
    for name in ("httpx", "httpcore", "urllib3", "websockets.client"):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(
    level: str = "INFO",
    *,
    log_file: str | None = None,
    max_mb: int = 10,
    backups: int = 5,
) -> None:
    """初始化根日志。

    - stdout：容器/前台友好；
    - log_file：可选，大小轮转文件（默认 10MB × 5 份），防止磁盘/内存被日志占满。
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    _quiet_noisy_loggers()

    formatter = JsonFormatter()
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max_mb * 1024 * 1024,
            backupCount=backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
