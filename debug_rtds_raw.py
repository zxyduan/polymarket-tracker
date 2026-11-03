#!/usr/bin/env python3
"""RTDS 事件流精确诊断（正确 URL 版）。

直接监听 RTDS 原始消息，统计：
- 收到多少条原始 WS 消息
- 解析出多少个事件
- 多少个事件的 wallet 在 watched 列表里
- watched 列表到底有哪些地址
"""

import asyncio
import json
import logging
import sys

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)

from src.sources.rtds_client import SUBSCRIBE_MESSAGE
from src.core.parsing import parse_rtds_message
from src.storage import Storage


async def diagnose():
    print("=" * 60)
    print("RTDS 原始消息诊断（监听 60 秒）")
    print("=" * 60)

    storage = Storage("data/monitor.db")
    await storage.init()

    watched = await storage.active_addresses()
    print(f"\n[watched 地址列表] ({len(watched)} 个)")
    for i, addr in enumerate(list(watched)[:10]):
        print(f"  {i+1}. {addr}")

    # 正确的 RTDS URL
    url = "wss://ws-live-data.polymarket.com"
    print(f"\n[连接 RTDS] {url}")

    raw_count = 0
    event_count = 0
    matched_count = 0
    sample_wallets = []

    import websockets

    async with websockets.connect(url, open_timeout=15, ping_interval=30) as ws:
        await ws.send(json.dumps(SUBSCRIBE_MESSAGE))
        print(f"✅ 已发送订阅消息: {json.dumps(SUBSCRIBE_MESSAGE)}")

        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < 60:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            raw_count += 1
            text = raw.decode() if isinstance(raw, bytes) else raw
            text = text.strip()
            if not text or text in ("PING", "pong", "PONG"):
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            events = list(parse_rtds_message(data))
            for evt in events:
                event_count += 1
                if len(sample_wallets) < 10:
                    sample_wallets.append(evt.wallet)
                if evt.wallet in watched:
                    matched_count += 1
                    print(f"  🎯 [匹配] wallet={evt.wallet[:20]}... type={evt.event_type} size={evt.size}")

            if raw_count % 50 == 0:
                elapsed = int(asyncio.get_event_loop().time() - start)
                print(f"  [{elapsed}s] 原始消息={raw_count} 事件={event_count} 匹配={matched_count}")

    print(f"\n{'='*60}")
    print(f"60 秒统计:")
    print(f"  RTDS 原始 WS 消息: {raw_count}")
    print(f"  解析出的事件: {event_count}")
    print(f"  匹配 watched 地址: {matched_count}")
    print()

    if event_count > 0:
        print(f"[前 10 个事件的 wallet 地址]")
        for i, w in enumerate(sample_wallets):
            in_watched = "✅ 在列表里" if w in watched else "❌ 不在列表里"
            print(f"  {i+1}. {w}  {in_watched}")

    if raw_count == 0:
        print("❌ RTDS 根本没发消息——URL 或订阅格式不对")
    elif event_count == 0:
        print("❌ 收到 WS 消息，但解析不出事件")
    elif matched_count == 0:
        print("❌ 有事件流，但 watched 地址里的人这 60 秒没成交")
        print("   对比上面的 wallet 地址和 watched 列表，看格式是否一致（大小写/checksum）")
    else:
        print("✅ 链路正常，事件匹配了")

    await storage.close()


if __name__ == "__main__":
    asyncio.run(diagnose())
