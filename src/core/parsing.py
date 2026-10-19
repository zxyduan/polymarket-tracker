"""外部报文 → 标准事件的解析层。

字段名在不同版本/端点上存在差异，本模块全部按"多候选键 + 缺失降级"处理；
报文级字段清单在联调阶段固化（校验报告 TBD-04）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.core.address import is_address, normalize
from src.core.models import (
    CONVERT,
    MERGE,
    REDEEM,
    REWARD,
    SPLIT,
    TRADE,
    StandardEvent,
)

# 各类字段候选键
_WALLET_KEYS = ("proxyWallet", "wallet", "walletAddress", "maker", "makerAddress", "user")
_TX_KEYS = ("txHash", "transactionHash", "tx_hash", "hash", "transaction_hash")
_LOG_KEYS = ("logIndex", "log_index")
_TIME_KEYS = ("timestamp", "time", "blockTime", "createdAt", "matchedAt")
_CONDITION_KEYS = ("conditionId", "condition_id", "market", "conditionID")
_TITLE_KEYS = ("title", "question", "marketQuestion", "marketTitle", "eventTitle")
_SLUG_KEYS = ("slug", "marketSlug", "market_slug", "eventSlug")
_ID_KEYS = ("id", "activityId", "_id", "tradeId")
_PRICE_KEYS = ("price", "limitPrice", "avgPrice")
_SIZE_KEYS = ("size", "amount", "matchedAmount", "quantity", "shares")
_OUTCOME_KEYS = ("outcome", "outcomeLabel", "tokenLabel")
_SIDE_KEYS = ("side", "takerSide")
_NOTIONAL_KEYS = ("usdcSize", "notional", "value", "usdcAmount", "payout")


def first_key(data: dict[str, Any], *keys: str) -> Any:
    """返回第一个非空字段值。"""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def to_decimal(value: Any) -> Decimal | None:
    """安全转 Decimal（字符串、数字均可）。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").removeprefix("$")
    if not text:
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001
        return None


def to_datetime(value: Any) -> datetime | None:
    """秒/毫秒时间戳或 ISO 字符串转 UTC datetime。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        if number > 1e12:  # 毫秒
            number /= 1000
        if number <= 0:
            return None
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dedupe_key(
    source: str,
    tx: str | None,
    log_index: Any,
    external_id: Any,
    wallet: str | None,
    side: str | None,
    outcome: str | None,
    price: Decimal | None,
    size: Decimal | None,
) -> str:
    tx_l = tx.lower() if tx else None
    if log_index is not None and str(log_index).isdigit():
        return f"{tx_l or 'notx'}:{log_index}"
    if tx_l and external_id:
        return f"{tx_l}:{external_id}"
    if external_id:
        return f"{source}:{external_id}"
    if tx_l:
        tail = hashlib.sha256(
            "|".join(
                [str(wallet), str(side), str(outcome), str(price), str(size)]
            ).encode()
        ).hexdigest()[:12]
        return f"{tx_l}:{tail}"
    tail = hashlib.sha256(
        "|".join(
            [str(wallet), str(side), str(outcome), str(price), str(size), str(external_id)]
        ).encode()
    ).hexdigest()[:16]
    return f"{source}:{tail}"


def build_trade(raw: dict[str, Any], source: str) -> StandardEvent | None:
    """把成交类原始报文归一为标准事件。"""
    wallet_raw = first_key(raw, *_WALLET_KEYS)
    wallet = normalize(str(wallet_raw)) if wallet_raw else None
    if not wallet or not is_address(wallet):
        return None

    price = to_decimal(first_key(raw, *_PRICE_KEYS))
    size = to_decimal(first_key(raw, *_SIZE_KEYS))
    notional = to_decimal(first_key(raw, *_NOTIONAL_KEYS))
    # 成交额兜底：size 是份额（RTDS/CLOB/Data 口径一致），notional = price × size。
    # 注：曾误判 RTDS size 为 USDC 金额，实测盘口对比证明 size 即份额。
    if notional is None and price is not None and size is not None:
        notional = price * size

    tx = first_key(raw, *_TX_KEYS)
    tx = str(tx) if tx else None
    log_index = first_key(raw, *_LOG_KEYS)
    external_id = first_key(raw, *_ID_KEYS)
    event_time = to_datetime(first_key(raw, *_TIME_KEYS))
    condition = first_key(raw, *_CONDITION_KEYS)
    title = first_key(raw, *_TITLE_KEYS)
    slug = first_key(raw, *_SLUG_KEYS)
    side = str(first_key(raw, *_SIDE_KEYS) or "").upper() or None
    outcome = first_key(raw, *_OUTCOME_KEYS)
    outcome = str(outcome).strip() if outcome is not None else None

    key = _dedupe_key(
        source,
        tx,
        log_index,
        external_id,
        wallet,
        side,
        outcome,
        price,
        size,
    )
    return StandardEvent(
        dedupe_key=key,
        source=source,
        wallet=wallet,
        event_type=TRADE,
        tx_hash=tx.lower() if tx else None,
        log_index=int(log_index) if log_index is not None and str(log_index).isdigit() else None,
        event_time=event_time,
        side=side,
        outcome=outcome,
        price=price,
        size=size,
        notional=notional,
        title=str(title) if title else None,
        slug=str(slug) if slug else None,
        condition_id=str(condition) if condition else None,
        external_id=str(external_id) if external_id is not None else None,
        raw=raw,
    )


def build_lifecycle(raw: dict[str, Any], source: str, event_type: str) -> StandardEvent | None:
    """把非成交类事件（SPLIT/MERGE/REDEEM/CONVERT/REWARD）归一。"""
    wallet_raw = first_key(raw, *_WALLET_KEYS)
    wallet = normalize(str(wallet_raw)) if wallet_raw else None
    if not wallet or not is_address(wallet):
        return None

    tx = first_key(raw, *_TX_KEYS)
    tx = str(tx) if tx else None
    log_index = first_key(raw, *_LOG_KEYS)
    external_id = first_key(raw, *_ID_KEYS)
    event_time = to_datetime(first_key(raw, *_TIME_KEYS))
    condition = first_key(raw, *_CONDITION_KEYS)
    title = first_key(raw, *_TITLE_KEYS)
    slug = first_key(raw, *_SLUG_KEYS)
    amount = to_decimal(first_key(raw, *_NOTIONAL_KEYS, *_SIZE_KEYS))

    key = _dedupe_key(
        source, tx, log_index, external_id, wallet, event_type, None, None, amount
    )
    return StandardEvent(
        dedupe_key=key,
        source=source,
        wallet=wallet,
        event_type=event_type,
        tx_hash=tx.lower() if tx else None,
        log_index=int(log_index) if log_index is not None and str(log_index).isdigit() else None,
        event_time=event_time,
        notional=amount,
        size=amount,
        title=str(title) if title else None,
        slug=str(slug) if slug else None,
        condition_id=str(condition) if condition else None,
        external_id=str(external_id) if external_id is not None else None,
        raw=raw,
    )


# ---- RTDS 消息解析 ----


def parse_rtds_message(raw: Any) -> list[StandardEvent]:
    """递归解析 RTDS 消息（支持信封、数组、orders_matched 内嵌成交）。"""
    events: list[StandardEvent] = []
    seen: set[str] = set()

    def _walk(node: Any, inherited: dict[str, Any]) -> list[StandardEvent]:
        found: list[StandardEvent] = []
        if isinstance(node, dict):
            # 外层信封的 txHash/timestamp 等字段向下继承，内层字段优先
            merged = {**inherited, **node}
            child_events: list[StandardEvent] = []
            for value in node.values():
                if isinstance(value, (dict, list)):
                    child_events.extend(_walk(value, merged))
            if child_events:
                found.extend(child_events)
            else:
                event = build_trade(merged, "rtds")
                if event is not None:
                    found.append(event)
        elif isinstance(node, list):
            for item in node:
                found.extend(_walk(item, inherited))
        return found

    for event in _walk(raw, {}):
        if event.dedupe_key not in seen:
            seen.add(event.dedupe_key)
            events.append(event)
    return events


# ---- Data API 消息解析 ----

_LIFECYCLE_MAP = {
    "SPLIT": SPLIT,
    "MERGE": MERGE,
    "REDEEM": REDEEM,
    "REDEMPTION": REDEEM,
    "CONVERT": CONVERT,
    "CONVERSION": CONVERT,
    "REWARD": REWARD,
    "REWARDS": REWARD,
}


def parse_data_trade(raw: dict[str, Any]) -> StandardEvent | None:
    """解析 Data API /trades 单项。"""
    return build_trade(raw, "data_api")


def parse_data_activity(raw: Any) -> list[StandardEvent]:
    """解析 Data API /activity 单项（可能为单对象或数组）。"""
    if isinstance(raw, list):
        events: list[StandardEvent] = []
        for item in raw:
            events.extend(parse_data_activity(item))
        return events
    if not isinstance(raw, dict):
        return []

    type_value = str(raw.get("type", "")).upper().strip()
    inner = raw.get("data")

    # 外层公共字段透传给内层
    outer_fields = {
        key: raw[key]
        for key in ("txHash", "timestamp", "conditionId", "title", "slug")
        if key in raw
    }

    if type_value == "TRADE":
        candidates = inner if isinstance(inner, list) else [inner if isinstance(inner, dict) else raw]
        result: list[StandardEvent] = []
        for item in candidates:
            merged = {**outer_fields, **item}
            event = build_trade(merged, "data_api")
            if event is not None:
                result.append(event)
        return result

    lifecycle = _LIFECYCLE_MAP.get(type_value)
    if lifecycle is not None:
        if isinstance(inner, dict):
            merged = {**outer_fields, **inner}
        else:
            merged = {**outer_fields, **raw}
        event = build_lifecycle(merged, "data_api", lifecycle)
        return [event] if event is not None else []

    # 未标注类型但本身像成交
    event = build_trade(raw, "data_api")
    return [event] if event is not None else []
