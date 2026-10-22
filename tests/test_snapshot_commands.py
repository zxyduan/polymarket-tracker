"""快照盘口动态管理（/add /remove）测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.settings import load_markets_toml
from src.snapshots.commands import SnapshotCommandHandler


class FakeCollector:
    """resolve_market 模拟：known 映射为 (cid, yes_token)。"""

    def __init__(self, known: dict[str, tuple[str, str]]) -> None:
        self.known = known

    async def resolve_market(self, slug: str) -> tuple[str, str] | None:
        return self.known.get(slug)


class FakeStorage:
    async def latest_complete_buckets(self, now_bucket: int) -> list[dict]:
        return []


def _make_handler(
    markets: list,
    markets_path: Path,
    known: dict[str, tuple[str, str]],
) -> SnapshotCommandHandler:
    return SnapshotCommandHandler(
        markets,
        FakeCollector(known),
        FakeStorage(),
        lambda: asyncio.sleep(0),  # trigger_snapshot 空实现
        {},
        markets_path=str(markets_path),
    )


def test_add_market_success(tmp_path: Path) -> None:
    markets: list = []
    handler = _make_handler(
        markets,
        tmp_path / "markets.toml",
        {"will-aliens-exist-in-2026": ("0xabc123", "111")},
    )
    reply = asyncio.run(handler("/add will-aliens-exist-in-2026 aliens", 1))
    assert reply is not None and "已新增盘口" in reply
    assert len(markets) == 1
    assert markets[0].slug_id == "aliens"
    assert markets[0].slug == "will-aliens-exist-in-2026"
    assert markets[0].condition_id == "0xabc123"
    # 落盘成功：文件存在且能重新加载
    entries = load_markets_toml(tmp_path / "markets.toml")
    assert len(entries) == 1
    assert entries[0].condition_id == "0xabc123"


def test_add_market_duplicate_rejected(tmp_path: Path) -> None:
    from src.settings import MarketEntry

    existing = [MarketEntry(slug_id="aliens", slug="will-aliens-exist-in-2026")]
    handler = _make_handler(
        existing,
        tmp_path / "markets.toml",
        {"will-aliens-exist-in-2026": ("0xabc123", "111")},
    )
    reply = asyncio.run(handler("/add will-aliens-exist-in-2026 aliens", 1))
    assert reply is not None and "已存在" in reply
    assert len(existing) == 1  # 未重复添加
    assert not (tmp_path / "markets.toml").exists()  # 未落盘


def test_add_market_resolve_failed(tmp_path: Path) -> None:
    markets: list = []
    handler = _make_handler(markets, tmp_path / "markets.toml", {})
    reply = asyncio.run(handler("/add no-such-market-xyz", 1))
    assert reply is not None and "解析失败" in reply
    assert len(markets) == 0
    assert not (tmp_path / "markets.toml").exists()


def test_add_market_missing_args(tmp_path: Path) -> None:
    markets: list = []
    handler = _make_handler(markets, tmp_path / "markets.toml", {})
    reply = asyncio.run(handler("/add", 1))
    assert reply is not None and "/add 用法" in reply


def test_remove_market_by_slug_id(tmp_path: Path) -> None:
    from src.settings import MarketEntry

    markets = [
        MarketEntry(slug_id="aliens", slug="will-aliens-exist-in-2026", condition_id="0xabc"),
        MarketEntry(slug_id="2027", slug="other-market", condition_id="0xdef"),
    ]
    handler = _make_handler(markets, tmp_path / "markets.toml", {})
    reply = asyncio.run(handler("/remove aliens", 1))
    assert reply is not None and "已删除盘口" in reply
    assert [m.slug_id for m in markets] == ["2027"]
    entries = load_markets_toml(tmp_path / "markets.toml")
    assert [e.slug_id for e in entries] == ["2027"]


def test_remove_market_by_slug(tmp_path: Path) -> None:
    from src.settings import MarketEntry

    markets = [MarketEntry(slug_id="aliens", slug="will-aliens-exist-in-2026")]
    handler = _make_handler(markets, tmp_path / "markets.toml", {})
    reply = asyncio.run(handler("/remove will-aliens-exist-in-2026", 1))
    assert reply is not None and "已删除盘口" in reply
    assert len(markets) == 0


def test_remove_market_not_found(tmp_path: Path) -> None:
    from src.settings import MarketEntry

    markets = [MarketEntry(slug_id="aliens", slug="will-aliens-exist-in-2026")]
    handler = _make_handler(markets, tmp_path / "markets.toml", {})
    reply = asyncio.run(handler("/remove nope", 1))
    assert reply is not None and "未找到" in reply
    assert len(markets) == 1


def test_save_markets_keeps_header_and_fields(tmp_path: Path) -> None:
    """落盘保留注释头与全部字段，可无损往返。"""
    from src.settings import MarketEntry, save_markets_toml

    p = tmp_path / "markets.toml"
    p.write_text(
        "# 市场快照监控列表\n"
        "# 注释保留测试\n"
        "\n"
        "[[markets]]\n"
        'slug_id       = "a"\n'
        'slug          = "market-a"\n'
        'condition_id  = "0x1"\n'
        'enabled       = true\n'
        'profile       = "one_way"\n'
        'fv_initial    = 0.05\n'
        'fv_start_date = ""\n'
        'fv_end_date   = "2027-01-01"\n',
        encoding="utf-8",
    )
    save_markets_toml(
        p,
        [
            MarketEntry(
                slug_id="a",
                slug="market-a",
                condition_id="0x1",
                profile="one_way",
                fv_initial=0.05,
                fv_end_date="2027-01-01",
            )
        ],
    )
    text = p.read_text(encoding="utf-8")
    assert "# 市场快照监控列表" in text
    entries = load_markets_toml(p)
    assert len(entries) == 1
    # 全字段往返无损
    assert entries[0].slug_id == "a"
    assert entries[0].condition_id == "0x1"
    assert entries[0].profile == "one_way"
    assert entries[0].fv_initial == 0.05
    assert entries[0].fv_end_date == "2027-01-01"
    assert (tmp_path / "markets.toml.bak").exists()  # 备份已写
