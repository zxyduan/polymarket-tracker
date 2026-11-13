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
        if m.condition_id:
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


def _yaml_double_quote(value: str) -> str:
    """把字符串序列化为 YAML 双引号标量。

    处理反斜杠、双引号与常见控制字符；普通 UTF-8（含中文）原样保留。
    """
    escapes = {
        "\\": "\\\\",
        '"': '\\"',
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\0": "\\0",
    }
    body = "".join(escapes.get(ch, ch) for ch in str(value))
    return f'"{body}"'


def _find_watchlist_span(lines: list[str]) -> tuple[int, int] | None:
    """返回顶层 watchlist 段的 [start, end) 行索引；段缺失返回 None。

    先定位 watchlist 之后第一个行首非空白、且非注释的行（下一个顶层 key），
    再把 end 向前回退，跳过段尾连续的空行与注释——它们属于段间/下一段，
    回写时保留，避免误删下一段的前置注释。
    """
    start = next(
        (i for i, ln in enumerate(lines) if ln.rstrip().startswith("watchlist:")),
        None,
    )
    if start is None:
        return None
    next_key = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if (
            ln.strip()
            and not ln.lstrip().startswith("#")
            and not ln[:1].isspace()
        ):
            next_key = j
            break
    # 回退：next_key 之前的空行与注释（段间/下一段前置注释）不纳入替换
    end = next_key
    while end > start + 1 and (
        not lines[end - 1].strip()
        or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    return start, end


def save_watchlist_yaml(
    path: str | Path, wallets: list[dict[str, Any]]
) -> None:
    """把当前 active 钱包文本级回写 config.yaml 的 watchlist 段。

    仅替换该段，其余内容（含注释、其他配置）原样保留；写前生成 .bak；
    watchlist 段缺失时在文件顶部插入。空名单写 'watchlist: []'。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.splitlines()
    trailing_newline = text.endswith("\n")

    if not wallets:
        new_block = ["watchlist: []"]
    else:
        new_block = ["watchlist:"]
        for w in wallets:
            new_block.append(
                f"  - address: {_yaml_double_quote(str(w['address']))}"
            )
            new_block.append(
                f"    label: {_yaml_double_quote(str(w.get('label', '') or ''))}"
            )

    span = _find_watchlist_span(lines)
    if span is None:
        block = list(new_block)
        if lines and lines[0].strip():
            block.append("")  # 与原首行之间空一行
        rebuilt = block + lines
    else:
        start, end = span
        rebuilt = lines[:start] + new_block + lines[end:]

    if p.exists():
        p.with_suffix(p.suffix + ".bak").write_text(text, encoding="utf-8")
    output = "\n".join(rebuilt)
    if trailing_newline:
        output += "\n"
    p.write_text(output, encoding="utf-8")


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

    def watchlist_target(self) -> tuple[bool, list[WatchlistSeed]]:
        """返回 (配置是否有效, 种子)。

        键缺失或显式 null -> 无效，调用方 fail fast、不做停用；
        显式空列表 -> 有效且为空（全停）；非空 -> 正常。
        """
        raw_val = self.raw.get("watchlist")
        if "watchlist" not in self.raw or raw_val is None:
            return False, []
        return True, self.watchlist_seeds

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

    @property
    def market_screen(self) -> dict[str, Any]:
        """market_screen 配置节（每日市场筛选）。"""
        return self.section("market_screen")

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


@dataclass(frozen=True)
class QueryDefaults:
    """持续性只读查询的默认参数（门面未显式传入时回落）。"""

    window_days: int = 10
    min_hit_days: int = 10
    min_volume_24h: float = 50000.0
    max_rank: int = 200
    min_remaining_days: int = 31
    prob_max: float = 0.15
    prob_side: str = "minority"
    require_consecutive: bool = False
    min_reward_daily_rate: float = 0.0


@dataclass(frozen=True)
class MarketScreenConfig:
    """market_screen 节解析结果（含校验输出，fail-soft）。

    内部拍平为扁平字段供用例/门面访问；原始 YAML 按设计 §9 嵌套
    `volume` / `horizon_prob` / `query_defaults` / `notify` 子节。
    """

    enabled: bool
    schedule: str
    schedule_hour: int
    schedule_minute: int
    timezone_offset_hours: int
    retry_seconds: int
    retention_days: int
    # volume 子节
    volume_metric: str
    min_value: float
    top_n: int
    page_limit: int
    max_pages: int
    request_timeout_seconds: int
    retry_attempts: int
    retry_backoff_seconds: float
    page_delay_seconds: float
    # horizon_prob 子节
    horizon_enabled: bool
    main_preset: str
    record_all_presets: bool
    mode: str
    clamp_min: float
    clamp_max: float
    custom_anchors: list[dict[str, float]] | None
    # 顶层开关
    binary_only: bool
    persist_non_passed: bool
    notify_enabled: bool
    # reward 子节
    reward_min_daily_rate: float
    reward_tier_low_min: float
    reward_tier_mid_min: float
    reward_tier_high_min: float
    query_defaults: QueryDefaults
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_VALID_PRESETS = ("strict", "balanced", "loose")


def _coerce_int(raw: Any, default: int, warnings: list[str], label: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        warnings.append(f"market_screen.{label} 非法: {raw!r}，回退 {default}")
        return default


def _coerce_float(raw: Any, default: float, warnings: list[str], label: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        warnings.append(f"market_screen.{label} 非法: {raw!r}，回退 {default}")
        return default


def resolve_market_screen_config(settings: Settings) -> MarketScreenConfig:
    """读取并校验 market_screen 节；致命错误不抛异常，返回 enabled=False + errors。"""
    raw = settings.market_screen
    vol = raw.get("volume") if isinstance(raw.get("volume"), dict) else {}
    hor = raw.get("horizon_prob") if isinstance(raw.get("horizon_prob"), dict) else {}
    qd_raw = raw.get("query_defaults") if isinstance(raw.get("query_defaults"), dict) else {}
    notify = raw.get("notify") if isinstance(raw.get("notify"), dict) else {}
    rw = raw.get("reward") if isinstance(raw.get("reward"), dict) else {}

    errors: list[str] = []
    warnings: list[str] = []

    enabled = bool(raw.get("enabled", True))

    # 调度时刻 HH:MM（08:00 默认）
    schedule = str(raw.get("schedule", "08:00")).strip()
    schedule_hour, schedule_minute = 8, 0
    try:
        h_str, m_str = schedule.split(":")
        schedule_hour, schedule_minute = int(h_str), int(m_str)
        if not (0 <= schedule_hour <= 23 and 0 <= schedule_minute <= 59):
            raise ValueError("out of range")
    except (ValueError, IndexError):
        errors.append(f"market_screen.schedule 非法: {schedule!r}（HH:MM）")
        schedule_hour, schedule_minute = 8, 0

    tz = raw.get("timezone_offset_hours", settings.get("display.timezone_offset_hours", 8))
    try:
        timezone_offset_hours = int(tz)
    except (TypeError, ValueError):
        warnings.append(f"market_screen.timezone_offset_hours 非法: {tz!r}，回退 8")
        timezone_offset_hours = 8
    if timezone_offset_hours != 8:
        warnings.append(
            "视图 B/C 动态剩余天数写死按 UTC+8；时区偏移分叉需 P2 可配化"
        )

    retry_seconds = _coerce_int(raw.get("retry_seconds", 5), 5, warnings, "retry_seconds")

    try:
        retention_days = int(raw.get("retention_days", 30))
    except (TypeError, ValueError):
        retention_days = 30
    if retention_days < 1:
        warnings.append("market_screen.retention_days 非法，回退 30")
        retention_days = 30

    # ── volume 子节 ──
    volume_metric = str(vol.get("metric", "volume24hr")).strip()
    if volume_metric != "volume24hr":
        warnings.append(f"volume.metric 仅支持 volume24hr，回退；收到 {volume_metric!r}")
        volume_metric = "volume24hr"

    min_value = _coerce_float(vol.get("min_value", 50000.0), 50000.0, warnings, "volume.min_value")
    if min_value < 0:
        errors.append("volume.min_value 不能为负")
        min_value = 50000.0

    top_n = _coerce_int(vol.get("top_n", 200), 200, warnings, "volume.top_n")
    if top_n < 1:
        errors.append("volume.top_n 必须 ≥ 1")
        top_n = 200
    page_limit = _coerce_int(vol.get("page_limit", 100), 100, warnings, "volume.page_limit")
    if page_limit < 1:
        errors.append("volume.page_limit 必须 ≥ 1")
        page_limit = 100
    max_pages = _coerce_int(vol.get("max_pages", 4), 4, warnings, "volume.max_pages")
    if page_limit * max_pages < top_n:
        warnings.append("volume.page_limit × max_pages 小于 top_n，候选可能截断在翻页上限")

    request_timeout_seconds = _coerce_int(
        vol.get("request_timeout_seconds", settings.get("polling.request_timeout_seconds", 10)),
        10, warnings, "volume.request_timeout_seconds",
    )
    if request_timeout_seconds <= 0:
        request_timeout_seconds = 10
    retry_attempts = _coerce_int(vol.get("retry_attempts", 2), 2, warnings, "volume.retry_attempts")
    if retry_attempts < 0:
        retry_attempts = 2
    retry_backoff_seconds = _coerce_float(
        vol.get("retry_backoff_seconds", 1.0), 1.0, warnings, "volume.retry_backoff_seconds"
    )
    if retry_backoff_seconds < 0:
        retry_backoff_seconds = 1.0
    page_delay_seconds = _coerce_float(
        vol.get("page_delay_seconds", 0.2), 0.2, warnings, "volume.page_delay_seconds"
    )
    if page_delay_seconds < 0:
        page_delay_seconds = 0.2

    # ── horizon_prob 子节 ──
    horizon_enabled = bool(hor.get("enabled", True))

    main_preset = str(hor.get("preset", "balanced")).strip()
    if main_preset not in _VALID_PRESETS:
        errors.append(f"horizon_prob.preset 非法: {main_preset!r}，须为 {_VALID_PRESETS}")
        main_preset = "balanced"

    record_all_raw = hor.get("record_all_presets", True)
    if isinstance(record_all_raw, bool):
        record_all_presets = record_all_raw
    else:
        warnings.append("horizon_prob.record_all_presets 非布尔，按 true 处理")
        record_all_presets = True

    mode = str(hor.get("mode", "anchor_linear")).strip()
    if mode != "anchor_linear":
        warnings.append("horizon_prob.mode 仅支持 anchor_linear（formula 预留 P2），已忽略")
        mode = "anchor_linear"

    clamp_min = _coerce_float(hor.get("clamp_min", 0.002), 0.002, warnings, "horizon_prob.clamp_min")
    clamp_max = _coerce_float(hor.get("clamp_max", 0.20), 0.20, warnings, "horizon_prob.clamp_max")
    if clamp_min >= clamp_max:
        warnings.append("horizon_prob.clamp_min 必须小于 clamp_max，已回退默认")
        clamp_min, clamp_max = 0.002, 0.20

    # 自定义锚点：单组列表整体覆盖主档模板；非法升序/范围记 errors 禁用任务。
    custom_anchors: list[dict[str, float]] | None = None
    raw_anchors = hor.get("anchors")
    if isinstance(raw_anchors, list) and raw_anchors:
        cleaned: list[dict[str, float]] = []
        prev_days = -1
        ok = True
        for item in raw_anchors:
            try:
                days = int(item["days"])
                minority_max = float(item["minority_max"])
            except (KeyError, TypeError, ValueError):
                ok = False
                break
            if days <= prev_days or not (0.0 < minority_max < 0.5):
                ok = False
                break
            prev_days = days
            cleaned.append({"days": days, "minority_max": minority_max})
        if ok:
            custom_anchors = cleaned
        else:
            errors.append(
                "horizon_prob.anchors 非法：须 days 严格升序且 minority_max∈(0,0.5)"
            )

    binary_only = bool(raw.get("binary_only", True))
    persist_non_passed = bool(raw.get("persist_non_passed", True))
    if not persist_non_passed:
        warnings.append(
            "persist_non_passed=false 会丢失未过主档候选历史，与持续性主场景冲突，建议保持 true"
        )
    notify_enabled = bool(notify.get("enabled", False))
    if notify_enabled:
        warnings.append("notify.enabled=true：P1 不装配推送通道，该开关暂不生效")

    # ── reward 子节：准入线 + 分档阈值 ──
    reward_min_daily_rate = _coerce_float(
        rw.get("min_daily_rate", 30.0), 30.0, warnings, "reward.min_daily_rate"
    )
    if reward_min_daily_rate < 0:
        warnings.append("reward.min_daily_rate 不能为负，回退 30.0")
        reward_min_daily_rate = 30.0

    tiers_raw = rw.get("tiers") if isinstance(rw.get("tiers"), dict) else {}
    tier_low = _coerce_float(tiers_raw.get("low_min", 30.0), 30.0, warnings, "reward.tiers.low_min")
    tier_mid = _coerce_float(tiers_raw.get("mid_min", 100.0), 100.0, warnings, "reward.tiers.mid_min")
    tier_high = _coerce_float(tiers_raw.get("high_min", 300.0), 300.0, warnings, "reward.tiers.high_min")
    if not (0.0 <= tier_low <= tier_mid <= tier_high):
        warnings.append("reward.tiers 阈值须非降序 0<=low<=mid<=high，回退 30/100/300")
        tier_low, tier_mid, tier_high = 30.0, 100.0, 300.0
    if reward_min_daily_rate > tier_low:
        warnings.append("reward.min_daily_rate 高于低档下限，低档区间部分不可达")

    # ── query_defaults 子节 ──
    try:
        prob_max = float(qd_raw.get("prob_max", 0.15))
    except (TypeError, ValueError):
        prob_max = 0.15
    if not (0.0 < prob_max < 0.5):
        warnings.append("query_defaults.prob_max 越界 (0,0.5)，回退 0.15")
        prob_max = 0.15
    prob_side = str(qd_raw.get("prob_side", "minority")).strip()
    if prob_side not in ("minority", "yes"):
        warnings.append("query_defaults.prob_side 非法，回退 minority")
        prob_side = "minority"
    require_consecutive = bool(qd_raw.get("require_consecutive", False))
    min_reward_daily_rate = _coerce_float(
        qd_raw.get("min_reward_daily_rate", 0.0), 0.0, warnings,
        "query_defaults.min_reward_daily_rate",
    )
    if min_reward_daily_rate < 0:
        warnings.append("query_defaults.min_reward_daily_rate 不能为负，回退 0.0")
        min_reward_daily_rate = 0.0

    query_defaults = QueryDefaults(
        window_days=_coerce_int(qd_raw.get("window_days", 10), 10, warnings, "query_defaults.window_days"),
        min_hit_days=_coerce_int(qd_raw.get("min_hit_days", 10), 10, warnings, "query_defaults.min_hit_days"),
        min_volume_24h=_coerce_float(qd_raw.get("min_volume_24h", 50000.0), 50000.0, warnings, "query_defaults.min_volume_24h"),
        max_rank=_coerce_int(qd_raw.get("max_rank", 200), 200, warnings, "query_defaults.max_rank"),
        min_remaining_days=_coerce_int(qd_raw.get("min_remaining_days", 31), 31, warnings, "query_defaults.min_remaining_days"),
        prob_max=prob_max,
        prob_side=prob_side,
        require_consecutive=require_consecutive,
        min_reward_daily_rate=min_reward_daily_rate,
    )

    return MarketScreenConfig(
        enabled=enabled and not errors,
        schedule=schedule,
        schedule_hour=schedule_hour,
        schedule_minute=schedule_minute,
        timezone_offset_hours=timezone_offset_hours,
        retry_seconds=retry_seconds,
        retention_days=retention_days,
        volume_metric=volume_metric,
        min_value=min_value,
        top_n=top_n,
        page_limit=page_limit,
        max_pages=max_pages,
        request_timeout_seconds=request_timeout_seconds,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        page_delay_seconds=page_delay_seconds,
        horizon_enabled=horizon_enabled,
        main_preset=main_preset,
        record_all_presets=record_all_presets,
        mode=mode,
        clamp_min=clamp_min,
        clamp_max=clamp_max,
        custom_anchors=custom_anchors,
        binary_only=binary_only,
        persist_non_passed=persist_non_passed,
        notify_enabled=notify_enabled,
        reward_min_daily_rate=reward_min_daily_rate,
        reward_tier_low_min=tier_low,
        reward_tier_mid_min=tier_mid,
        reward_tier_high_min=tier_high,
        query_defaults=query_defaults,
        errors=errors,
        warnings=warnings,
    )
