"""脱敏样本数据（结构以联调实测为准，TBD-04）。"""

from __future__ import annotations

W1 = "0x1111111111111111111111111111111111111111"
W2 = "0x2222222222222222222222222222222222222222"
W3 = "0x3333333333333333333333333333333333333333"
W4 = "0x4444444444444444444444444444444444444444"
W5 = "0x5555555555555555555555555555555555555555"

RTDS_TRADE = {
    "type": "trade",
    "proxyWallet": W1,
    "side": "BUY",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "price": 0.632,
    "size": 1500,
    "title": "Will X happen before 2027?",
    "slug": "will-x-happen-before-2027",
    "conditionId": "0xaaa",
    "txHash": "0xabc123",
    "logIndex": 7,
    "timestamp": 1781000000,
}

RTDS_ORDERS_MATCHED = {
    "type": "orders_matched",
    "txHash": "0xdef456",
    "timestamp": 1781000100,
    "makerTrades": [
        {
            "proxyWallet": W2,
            "side": "SELL",
            "outcome": "Yes",
            "price": 0.5,
            "size": 100,
            "logIndex": 2,
            "conditionId": "0xbbb",
            "title": "Q1?",
        },
        {
            "proxyWallet": W2,
            "side": "SELL",
            "outcome": "Yes",
            "price": 0.5,
            "size": 100,
            "logIndex": 3,
            "conditionId": "0xbbc",
            "title": "Q2?",
        },
    ],
    "takerTrades": [
        {
            "proxyWallet": W3,
            "side": "BUY",
            "outcome": "Yes",
            "price": 0.5,
            "size": 200,
            "logIndex": 4,
            "conditionId": "0xbbb",
            "title": "Q1?",
        }
    ],
}

DATA_TRADES_ITEM = {
    "proxyWallet": W5,
    "side": "SELL",
    "outcome": "Yes",
    "price": 0.7,
    "size": 200,
    "txHash": "0x777",
    "logIndex": 5,
    "timestamp": 1781000200,
    "conditionId": "0xddd",
    "title": "Q4?",
}

DATA_ACTIVITY_TRADE = {
    "type": "TRADE",
    "txHash": "0x999",
    "timestamp": "2026-10-19T10:00:00Z",
    "data": {
        "proxyWallet": W4,
        "side": "BUY",
        "outcome": "No",
        "price": 0.3,
        "size": 500,
        "logIndex": 1,
        "conditionId": "0xccc",
        "title": "Q3?",
    },
}

DATA_ACTIVITY_REDEEM = {
    "type": "REDEEM",
    "txHash": "0x888",
    "timestamp": "2026-10-19T11:00:00Z",
    "data": {
        "proxyWallet": W4,
        "conditionId": "0xccc",
        "title": "Q3?",
        "usdcSize": 1000,
    },
}
