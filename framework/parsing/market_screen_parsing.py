"""Market Screen 纯函数层（设计 v3.5 §3.4/3.5、§4）。

本模块无 IO：报文解析、剩余天数、锚点归一化、线性插值、三档布尔判定。
preset 选择终态合并、量阈值过滤、TopN 截断在 usecase 完成；本层不做。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from framework.models.market_screen import CandidateMarket

PRICE_SUM_TOLERANCE: float = 0.01

# 三档内置模板锚点 `(days, minority_max)`，按 days 升序；值越小越严谨。
PRESET_ANCHORS: dict[str, tuple[tuple[int, float], ...]] = {
    "strict": ((30, 0.005), (60, 0.01), (365, 0.05)),
    "balanced": ((30, 0.01), (60, 0.02), (365, 0.10)),
    "loose": ((30, 0.02), (60, 0.035), (365, 0.15)),
}

PRESETS: tuple[str, ...] = ("strict", "balanced", "loose")


@dataclass(frozen=True)
class PresetEvaluation:
    """三档判定结果（parsing 内部结构，不作为跨层 DTO 进入 models 层）。"""

    horizon_pass: int
    threshold_lo: float | None
    pass_strict: int | None
    pass_balanced: int | None
    pass_loose: int | None


# ── 数值 / 结构安全解析 ────────────────────────────────────────────────────


def _json_list(value: Any) -> list[Any] | None:
    """兼容 list 与 JSON 字符串数组；解析失败或非 list 返回 None。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # 兼容逗号分隔的非 JSON 字符串（如 "Yes,No"）
            parsed = [part.strip() for part in text.split(",") if part.strip()]
        return parsed if isinstance(parsed, list) else None
    return None


def _to_float(value: Any) -> float | None:
    """安全转 float；None / 布尔 / 非数值 / 无穷 / NaN 一律返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _parse_date_part(value: Any) -> str | None:
    """从 endDateIso / endDate 提取归一 `YYYY-MM-DD`；无法解析返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    head = text.split("T", 1)[0].split(" ", 1)[0].replace("/", "-")
    parts = head.split("-")
    if len(parts) == 3:
        try:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            return date(y, m, d).isoformat()
        except ValueError:
            return None
    return None


# ── 剩余天数 ──────────────────────────────────────────────────────────────


def calc_remaining_days(
    end_date_iso: str, now: datetime, tz_offset_hours: int
) -> int | None:
    """剩余日历天：ceil((到期当日结束 − now)/86400)；<=0 返回 None。

    `now` 由调用方传入便于单测；naive 时间按配置偏移解释，aware 时间换算到该偏移。
    """
    try:
        end_date = date.fromisoformat(end_date_iso)
    except ValueError:
        return None
    tz = timezone(timedelta(hours=tz_offset_hours))
    if now.tzinfo is None:
        now_local = now.replace(tzinfo=tz)
    else:
        now_local = now.astimezone(tz)
    end_end = datetime.combine(end_date, time.max, tzinfo=tz)
    diff_seconds = (end_end - now_local).total_seconds()
    remaining = math.ceil(diff_seconds / 86400)
    return remaining if remaining > 0 else None


# ── 锚点与插值 ────────────────────────────────────────────────────────────


def resolve_anchors(
    preset: str,
    custom_anchors: list[dict[str, float]] | None,
) -> tuple[tuple[int, float], ...]:
    """自定义锚点非空且合法时整体覆盖 preset 模板；否则回退内置模板。

    非法情形（非升序、值越界、字段缺失）在配置解析层已拦截；本函数做防御性
    回退，不抛业务异常。
    """
    if custom_anchors:
        try:
            anchors = tuple(
                (int(item["days"]), float(item["minority_max"]))
                for item in custom_anchors
            )
            valid = bool(anchors) and all(
                anchors[i][0] < anchors[i + 1][0]
                and 0.0 < anchors[i][1] < 0.5
                for i in range(len(anchors) - 1)
            )
            if valid:
                return anchors
        except (KeyError, TypeError, ValueError):
            pass
    return PRESET_ANCHORS.get(preset, PRESET_ANCHORS["balanced"])


def interpolate_minority_max(
    days: int,
    anchors: tuple[tuple[int, float], ...],
    *,
    below_first: str = "nearest",
    above_last: str = "clamp",
    clamp_min: float = 0.002,
    clamp_max: float = 0.20,
) -> float:
    """按剩余天数在锚点间线性插值，输出小侧概率上限；边界 nearest/clamp 后裁剪。"""
    if not anchors:
        lo = 0.10
    elif days <= anchors[0][0]:
        lo = anchors[0][1] if below_first == "nearest" else anchors[0][1]
    elif days >= anchors[-1][0]:
        lo = anchors[-1][1] if above_last == "clamp" else anchors[-1][1]
    else:
        lo = anchors[-1][1]
        for i in range(len(anchors) - 1):
            d0, v0 = anchors[i]
            d1, v1 = anchors[i + 1]
            if d0 < days <= d1:
                ratio = (days - d0) / (d1 - d0) if d1 > d0 else 0.0
                lo = v0 + (v1 - v0) * ratio
                break
    return min(clamp_max, max(clamp_min, lo))


def evaluate_presets(
    remaining_days: int,
    minority_prob: float,
    anchors_by_preset: dict[str, tuple[tuple[int, float], ...]],
    *,
    record_all: bool,
    main_preset: str,
    clamp_min: float,
    clamp_max: float,
) -> PresetEvaluation:
    """计算主档与（可选）三档并行布尔标志；判定式 minority <= lo（Yes/No 对称）。"""

    def _pass(preset: str) -> tuple[int | None, float | None]:
        anchors = anchors_by_preset.get(preset)
        if anchors is None:
            return None, None
        lo = interpolate_minority_max(
            remaining_days, anchors, clamp_min=clamp_min, clamp_max=clamp_max
        )
        return (1 if minority_prob <= lo else 0), lo

    main_pass, main_lo = _pass(main_preset)
    if record_all:
        strict_pass, _ = _pass("strict")
        balanced_pass, _ = _pass("balanced")
        loose_pass, _ = _pass("loose")
    else:
        strict_pass = balanced_pass = loose_pass = None

    return PresetEvaluation(
        horizon_pass=main_pass if main_pass is not None else 0,
        threshold_lo=main_lo,
        pass_strict=strict_pass,
        pass_balanced=balanced_pass,
        pass_loose=loose_pass,
    )


# ── 报文解析 ───────────────────────────────────────────────────────────────


def parse_market(
    raw: dict[str, object],
    *,
    now: datetime,
    tz_offset_hours: int,
) -> CandidateMarket | None:
    """解析 Gamma 原始条目为候选 DTO；脏数据返回 None（不抛异常、不写假候选）。

    剔除：缺 conditionId、非 Yes/No 二元、价格缺失/非数值/价格和偏离 1、
    24h 量缺失/非数值、缺结束时间、已到期。
    """
    if not isinstance(raw, dict):
        return None

    cid = raw.get("conditionId")
    if cid is None or str(cid).strip() == "":
        return None
    cid = str(cid).strip().lower()

    outcomes = _json_list(raw.get("outcomes"))
    if not outcomes:
        return None
    labels = [str(item).strip().lower() for item in outcomes]
    if "yes" not in labels or "no" not in labels:
        return None
    yes_idx = labels.index("yes")
    no_idx = labels.index("no")
    if yes_idx == no_idx:
        return None

    prices = _json_list(raw.get("outcomePrices"))
    if not prices or len(prices) != 2:
        return None
    p_yes = _to_float(prices[yes_idx])
    p_no = _to_float(prices[no_idx])
    if p_yes is None:
        return None
    if p_no is None:
        p_no = 1.0 - p_yes
    if not (0.0 <= p_yes <= 1.0 and 0.0 <= p_no <= 1.0):
        return None
    if abs(p_yes + p_no - 1.0) > PRICE_SUM_TOLERANCE:
        return None
    minority = min(p_yes, p_no)

    volume = _to_float(raw.get("volume24hr"))
    if volume is None:
        return None

    end_iso = _parse_date_part(raw.get("endDateIso"))
    end_raw = str(raw.get("endDate") or "")
    if end_iso is None:
        end_iso = _parse_date_part(raw.get("endDate"))
    if end_iso is None:
        return None
    remaining = calc_remaining_days(end_iso, now, tz_offset_hours)
    if remaining is None:
        return None

    slug = str(raw.get("slug") or "").strip()
    question = str(raw.get("question") or raw.get("title") or "").strip()

    return CandidateMarket(
        run_date="",
        condition_id=cid,
        slug=slug,
        question=question,
        outcomes_json=json.dumps(["Yes", "No"]),
        end_date_iso=end_iso,
        end_date_raw=end_raw,
        remaining_days=remaining,
        prob_yes=p_yes,
        prob_no=p_no,
        minority_prob=minority,
        volume_24h=volume,
        volume_metric="volume24hr",
        rank_no=0,
        horizon_preset="balanced",
        threshold_lo=None,
        horizon_pass=0,
        pass_strict=None,
        pass_balanced=None,
        pass_loose=None,
        screen_passed=0,
        screen_reason="",
        collected_ts=0.0,
    )
