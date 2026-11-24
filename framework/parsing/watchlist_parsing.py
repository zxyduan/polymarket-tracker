"""白名单市场奖励解析与两轮 diff 纯函数（T2）。

无 IO、可单测；数值容错 / 代币映射 / 分档复用 `market_screen_parsing`。
CLOB raw 结构（`rewards_config[]`）与 Gamma `clobRewards` 字段名不同，
本模块按 raw 结构单独适配，不套 Gamma 解析。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from framework.models.watchlist_alert import RewardChangeEvent
from framework.parsing.market_screen_parsing import (
    _REWARD_ASSET_SYMBOLS,
    _to_float,
    classify_reward_tier,
)

# 档位层级序：high > mid > low；None 档不参与下滑判定。
_TIER_RANK: dict[str, int] = {"high": 3, "mid": 2, "low": 1}


@dataclass(frozen=True)
class ParsedReward:
    """单市场本轮解析出的奖励段。"""

    rate: float | None
    asset: str
    start_date: str
    end_date: str
    tier: str | None


def _json_list(value: Any) -> list[Any] | None:
    """兼容 list 与 JSON 字符串数组；解析失败或非 list 返回 None。

    与 `market_screen_parsing._json_list` 同款小容错，本模块独立持有，
    不改写原模块私有函数签名。
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = [part.strip() for part in text.split(",") if part.strip()]
        return parsed if isinstance(parsed, list) else None
    return None


def parse_clob_reward(
    raw: dict[str, object],
    *,
    low_min: float,
    mid_min: float,
    high_min: float,
) -> ParsedReward | None:
    """从 CLOB raw 的 `rewards_config[0]` 抽取；空 / 非 dict / 全脏值返回 None。"""
    try:
        entries = _json_list(raw.get("rewards_config"))
        if not entries:
            return None
        first = entries[0]
        if not isinstance(first, dict):
            return None

        rate = _to_float(first.get("rate_per_day"))
        address = str(first.get("asset_address") or "").strip().lower()
        asset = _REWARD_ASSET_SYMBOLS.get(address, address) if address else ""
        start_date = str(first.get("start_date") or "").strip()
        end_date = str(first.get("end_date") or "").strip()
        if rate is None:
            # 条目存在但速率脏值 / 缺失：视为无有效奖励段。
            return None
        tier = classify_reward_tier(
            rate, low_min=low_min, mid_min=mid_min, high_min=high_min
        )
        return ParsedReward(
            rate=rate,
            asset=asset,
            start_date=start_date,
            end_date=end_date,
            tier=tier,
        )
    except Exception:  # noqa: BLE001 - 奖励为附加字段：任何解析异常都收敛为 None
        return None


def _tier_lower(cur: str | None, prev: str | None) -> bool:
    """cur 层级是否低于 prev；任一 None 不参与下滑判定。"""
    if cur is None or prev is None:
        return False
    return _TIER_RANK.get(cur, 0) < _TIER_RANK.get(prev, 0)


def diff_rewards(
    prev: ParsedReward | None,
    cur: ParsedReward | None,
    *,
    cid: str,
    min_daily_rate: float,
    decay_pct_threshold: float,
    collected_ts: datetime | None = None,
) -> list[RewardChangeEvent]:
    """返回本轮命中的原始事件列表；连续确认 / 冷却 / 去重在用例层处理。"""
    events: list[RewardChangeEvent] = []
    prev_rate = prev.rate if prev is not None else None
    cur_rate = cur.rate if cur is not None else None
    prev_tier = prev.tier if prev is not None else None
    cur_tier = cur.tier if cur is not None else None
    asset = cur.asset if cur is not None else (prev.asset if prev is not None else "")
    prev_date = prev.end_date if prev is not None else ""
    cur_date = cur.end_date if cur is not None else ""

    def _make(code: str) -> RewardChangeEvent:
        return RewardChangeEvent(
            cid=cid,
            event_code=code,
            prev_rate=prev_rate,
            cur_rate=cur_rate,
            prev_tier=prev_tier,
            cur_tier=cur_tier,
            asset=asset,
            prev_date=prev_date,
            cur_date=cur_date,
            collected_ts=collected_ts,
        )

    prev_online = prev_rate is not None and prev_rate >= min_daily_rate
    cur_online = cur_rate is not None and cur_rate >= min_daily_rate

    # exit：上一轮在线，本轮奖励段消失（None）或速率归零。
    if prev_online and (cur_rate is None or cur_rate == 0):
        events.append(_make("exit"))
    # floor_break：本轮有奖励段但速率低于准入线（0 < rate < min）。
    elif cur_rate is not None and 0 < cur_rate < min_daily_rate:
        events.append(_make("floor_break"))

    # decay_pct：两轮均在线且 prev.rate > 0，相对降幅达阈值；prev ≤ 0 不参与（防除零）。
    if (
        prev_online
        and cur_online
        and prev_rate is not None
        and prev_rate > 0
        and cur_rate is not None
    ):
        drop_pct = (prev_rate - cur_rate) / prev_rate
        if drop_pct >= decay_pct_threshold:
            events.append(_make("decay_pct"))

    # tier_drop：两轮均有奖励，当前档位低于上一档。
    if prev is not None and cur is not None and _tier_lower(cur_tier, prev_tier):
        events.append(_make("tier_drop"))

    # rebound：上一轮退出 / 跌破线，本轮回到准入线以上（P1 仅记日志，不入推送）。
    if (not prev_online) and cur_online:
        events.append(_make("rebound"))

    return events
