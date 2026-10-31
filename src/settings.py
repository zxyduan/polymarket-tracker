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


def save_markets_toml(path: str | Path, entries: list[MarketEntry]) -> None:
    """把市场列表整体写回 markets.toml（/add /remove 落盘用）。

    保留原始头部注释，仅重写 [[markets]] 数组；字段全部序列化，
    避免下次 load 丢字段。写前先备份 .bak，防异常中断丢配置。
    """
    p = Path(path)
    header: list[str] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("[[markets]]"):
                break
            header.append(line)
    backup = p.with_suffix(p.suffix + ".bak")
    if p.exists():
        backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")

    lines = list(header)
    while lines and not lines[-1].strip():
        lines.pop()  # 去掉头部末尾空行，统一格式
    lines.append("")
    for m in entries:
        lines.append("[[markets]]")
        lines.append(f'slug_id       = "{_toml_escape(m.slug_id)}"')
        lines.append(f'slug          = "{_toml_escape(m.slug)}"')
        lines.append(f'condition_id  = "{_toml_escape(m.condition_id)}"')
        lines.append(f"enabled       = {str(bool(m.enabled)).lower()}")
        lines.append(f'profile       = "{_toml_escape(m.profile)}"')
        lines.append(f"fv_initial    = {m.fv_initial:.6g}")
        lines.append(f'fv_start_date = "{_toml_escape(m.fv_start_date)}"')
        lines.append(f'fv_end_date   = "{_toml_escape(m.fv_end_date)}"')
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def _toml_escape(value: str) -> str:
    """TOML 基础字符串转义（反斜杠与双引号）。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


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
    def daily_bot_token(self) -> str:
        """独立日报 Bot token（环境变量 DAILY_BOT_TOKEN；空 = 复用快照 Bot）。"""
        return os.getenv("DAILY_BOT_TOKEN", "").strip()

    @property
    def daily_report(self) -> dict[str, Any]:
        """daily_report 配置节（日报调度与展示参数）。"""
        return self.section("daily_report")

    @property
    def notify_platforms(self) -> list[str]:
        """推送平台列表（可多个同时启用）：telegram | ntfy，缺省 ["telegram"]。

        支持两种写法：
        - 新：notify.platforms: ["ntfy", "telegram"]  ← 多平台同时推送
        - 旧：notify.platform: "ntfy"                 ← 单平台（自动包成列表）
        后续扩展其他平台时在此枚举上增加实现（见 src/notify/factory）。
        """
        raw_val = self.get("notify.platforms", self.get("notify.platform", "telegram"))
        if isinstance(raw_val, list):
            values = [str(v).strip().lower() for v in raw_val]
        else:
            values = [str(raw_val).strip().lower()]
        return [v for v in values if v]

    @property
    def notify_platform(self) -> str:
        """单平台语义（兼容旧调用方）：取平台列表第一个，缺省 telegram。"""
        return self.notify_platforms[0] if self.notify_platforms else "telegram"

    @property
    def ntfy(self) -> dict[str, Any]:
        """ntfy 通道配置节（server/topic/access_token/…）。"""
        return self.section("ntfy")

    @property
    def snapshot_push(self) -> dict[str, Any]:
        """snapshot_push 配置节（推送调度）。"""
        return self.section("snapshot_push")

    @property
    def snapshot(self) -> dict[str, Any]:
        """snapshot 配置节（采集参数）。"""
        return self.section("snapshot")

    @property
    def digest(self) -> dict[str, Any]:
        """digest 配置节（交易员告警批量摘要）。"""
        return self.section("digest")

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


@dataclass(frozen=True)
class DailyReportConfig:
    """daily_report 节解析结果（含校验输出）。"""

    enabled: bool
    time_hour: int
    time_minute: int
    timezone_offset_hours: int
    windows_days: list[int]
    bucket_seconds: int
    top_n: int
    page_size: int
    segment_delay_seconds: float
    dedup: bool
    errors: list[str] = field(default_factory=list)      # 致命：禁用日报任务
    warnings: list[str] = field(default_factory=list)    # 不阻断，仅日志


def resolve_daily_config(settings: Settings) -> DailyReportConfig:
    """读取并校验 daily_report 节。

    致命错误不抛异常，返回 enabled=False + errors，由 amain 层跳过任务
    注册（fail-fast 仅禁用本任务，不拖垮主服务）。
    """
    raw = settings.daily_report
    errors: list[str] = []
    warnings: list[str] = []

    enabled = bool(raw.get("enabled", True))

    # time 解析（风格对齐 main._parse_snapshot_schedule；拒绝 24:00）
    time_str = str(raw.get("time", "07:00")).strip()
    time_hour, time_minute = 7, 0
    try:
        h_str, m_str = time_str.split(":")
        time_hour, time_minute = int(h_str), int(m_str)
        if not (0 <= time_hour <= 23 and 0 <= time_minute <= 59):
            raise ValueError("hour/minute out of range")
    except (ValueError, IndexError):
        errors.append(
            f"daily_report.time 非法: {time_str!r}（HH:MM，0–23 时，不支持 24:00）"
        )
        time_hour, time_minute = 7, 0

    tz = raw.get(
        "timezone_offset_hours",
        settings.get("display.timezone_offset_hours", 8),
    )
    try:
        timezone_offset_hours = int(tz)
    except (TypeError, ValueError):
        errors.append(f"daily_report.timezone_offset_hours 非法: {tz!r}")
        timezone_offset_hours = 8

    try:
        windows_days = [int(x) for x in (raw.get("windows_days") or [1, 3, 7])]
    except (TypeError, ValueError):
        errors.append("daily_report.windows_days 必须为整数列表")
        windows_days = []
    if not windows_days or any(d <= 0 for d in windows_days) or (
        windows_days != sorted(windows_days)
    ):
        errors.append("daily_report.windows_days 必须为非空正整数且严格升序")

    # bucket_seconds 必须纳入统一错误收集：非数字 / 0 / 负数都不能抛异常拖垮
    # 主服务（fail-fast 仅禁用日报）；取值后先判正整数，再参与整除/取模判断。
    try:
        bucket_seconds = int(
            raw.get("bucket_seconds", settings.snapshot.get("bucket_seconds", 7200))
        )
    except (TypeError, ValueError) as exc:
        errors.append(f"daily_report.bucket_seconds 非法: {exc}")
        bucket_seconds = 7200
    if bucket_seconds <= 0:
        errors.append("daily_report.bucket_seconds 必须为正整数")
        bucket_seconds = 7200

    if windows_days:
        if any((d * 86400) % bucket_seconds != 0 for d in windows_days):
            errors.append("daily_report 窗口天数必须能被 bucket_seconds 整除")
        snap_bucket = settings.snapshot.get("bucket_seconds")
        if snap_bucket is not None:
            # 快照节自身非法由快照配置校验负责；这里只做容忍性比较，不重复抛错。
            try:
                if int(snap_bucket) != bucket_seconds:
                    warnings.append(
                        "daily_report.bucket_seconds 与 snapshot.bucket_seconds 不一致"
                    )
            except (TypeError, ValueError):
                pass

    try:
        top_n = int(raw.get("top_n", 0))
        page_size = int(raw.get("page_size", 20))
        segment_delay_seconds = float(raw.get("segment_delay_seconds", 0.4))
    except (TypeError, ValueError) as exc:
        errors.append(f"daily_report 数值参数非法: {exc}")
        top_n, page_size, segment_delay_seconds = 0, 20, 0.4
    dedup = bool(raw.get("dedup", True))

    if top_n < 0:
        errors.append("daily_report.top_n 不能为负（0 = 不限制）")
    if page_size < 1:
        errors.append("daily_report.page_size 必须 ≥ 1")
    if segment_delay_seconds < 0:
        errors.append("daily_report.segment_delay_seconds 不能为负")

    # 保留期警告用 prune_loop 实际生效值（settings.snapshot.retention_days）；
    # collector.RETENTION_DAYS 是采集器内部第二套清理口径，本校验不读它。
    # 同样纳入容错：快照节保留期非数字时回退 90，不向启动流程抛异常。
    try:
        retention_days = int(settings.snapshot.get("retention_days", 90))
    except (TypeError, ValueError):
        retention_days = 90
    if retention_days <= 0:
        retention_days = 90
    if windows_days and bucket_seconds > 0 and max(windows_days) * 86400 // bucket_seconds > (
        retention_days * 86400 // bucket_seconds
    ):
        warnings.append("最长窗口超过快照保留期，早期桶可能已被清理")

    return DailyReportConfig(
        enabled=enabled and not errors,
        time_hour=time_hour,
        time_minute=time_minute,
        timezone_offset_hours=timezone_offset_hours,
        windows_days=windows_days,
        bucket_seconds=bucket_seconds,
        top_n=top_n,
        page_size=page_size,
        segment_delay_seconds=segment_delay_seconds,
        dedup=dedup,
        errors=errors,
        warnings=warnings,
    )
