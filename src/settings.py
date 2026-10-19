"""配置加载与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


@dataclass(frozen=True)
class WatchlistSeed:
    """配置文件中的监控地址种子。"""

    address: str
    label: str = ""


@dataclass(frozen=True)
class MarketEntry:
    """快照监控市场（config/markets.toml 的 [[markets]]）。"""

    slug_id: str
    slug: str
    condition_id: str = ""
    enabled: bool = True
    profile: str = ""
    fv_initial: float = 0.0
    fv_start_date: str = ""
    fv_end_date: str = ""


def load_markets_toml(path: str | Path) -> list[MarketEntry]:
    """加载 markets.toml 的 [[markets]] 数组。文件缺失返回空列表。"""
    p = Path(path)
    if not p.exists():
        return []
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    entries: list[MarketEntry] = []
    for item in data.get("markets", []) or []:
        slug = str(item.get("slug", "")).strip()
        if not slug:
            continue
        entries.append(
            MarketEntry(
                slug_id=str(item.get("slug_id", "")).strip() or slug,
                slug=slug,
                condition_id=str(item.get("condition_id", "")).strip(),
                enabled=bool(item.get("enabled", True)),
                profile=str(item.get("profile", "")).strip(),
                fv_initial=float(item.get("fv_initial", 0.0) or 0.0),
                fv_start_date=str(item.get("fv_start_date", "")).strip(),
                fv_end_date=str(item.get("fv_end_date", "")).strip(),
            )
        )
    return entries


@dataclass
class Settings:
    """运行期配置（环境变量 + config.yaml）。"""

    bot_token: str
    allowed_chat_ids: list[int]
    log_level: str
    config_path: Path
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, config_path: str | Path = "config/config.yaml") -> "Settings":
        """加载 .env 与 YAML 配置并校验。"""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        raw_chats = os.getenv("ALLOWED_CHAT_IDS", "").strip()
        chat_ids = [
            int(item.strip())
            for item in raw_chats.split(",")
            if item.strip().lstrip("-").isdigit()
        ]
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            allowed_chat_ids=chat_ids,
            log_level=log_level,
            config_path=path,
            raw=raw,
        )

    @property
    def watchlist_seeds(self) -> list[WatchlistSeed]:
        """配置文件中的监控地址列表（支持多个）。"""
        seeds: list[WatchlistSeed] = []
        for item in self.raw.get("watchlist", []) or []:
            address = str(item.get("address", "")).strip()
            if address:
                seeds.append(
                    WatchlistSeed(address=address, label=str(item.get("label", "")).strip())
                )
        return seeds

    @property
    def markets(self) -> list[MarketEntry]:
        """快照监控市场列表（config/markets.toml）。"""
        path = self.config_path.parent / "markets.toml"
        return [m for m in load_markets_toml(path) if m.enabled]

    @property
    def snapshot_bot_token(self) -> str:
        return os.getenv("SNAPSHOT_BOT_TOKEN", "").strip()

    @property
    def snapshot_push(self) -> dict[str, Any]:
        """snapshot_push 配置节（推送调度）。"""
        return self.section("snapshot_push")

    @property
    def snapshot(self) -> dict[str, Any]:
        """snapshot 配置节（采集参数）。"""
        return self.section("snapshot")

    def section(self, name: str) -> dict[str, Any]:
        """取一个配置节，缺失时返回空 dict。"""
        return dict(self.raw.get(name, {}) or {})

    def get(self, path: str, default: Any = None) -> Any:
        """按点分路径取配置，如 endpoints.rtds。"""
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
