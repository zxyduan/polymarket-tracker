"""指令处理测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.bot.commands import CommandHandler
from src.storage import Storage
from tests.fixtures import samples as s


@pytest.fixture
async def handler(tmp_path: Path) -> CommandHandler:
    storage = Storage(str(tmp_path / "c.db"))
    await storage.init()
    await storage.add_wallet(s.W1, "交易员A")
    return CommandHandler(storage, {"rtds_connected": True, "l2_last_run": "10-19 10:00"})


async def test_help(handler: CommandHandler) -> None:
    reply = await handler("/help", 1)
    assert reply is not None and "market" in reply


async def test_add_invalid(handler: CommandHandler) -> None:
    reply = await handler("/add 0x123", 1)
    assert "格式错误" in reply


async def test_add_valid(handler: CommandHandler) -> None:
    reply = await handler(f"/add {s.W2} 交易员B", 1)
    assert "已加入" in reply
    reply_dup = await handler(f"/add {s.W2} 交易员B", 1)
    assert "已在监控" in reply_dup


async def test_list(handler: CommandHandler) -> None:
    reply = await handler("/list", 1)
    assert "交易员A" in reply


async def test_status(handler: CommandHandler) -> None:
    reply = await handler("/status", 1)
    assert "已连接" in reply
    assert "监控地址数" in reply


async def test_remove(handler: CommandHandler) -> None:
    reply = await handler(f"/remove {s.W1}", 1)
    assert "已移出" in reply
    reply_again = await handler(f"/remove {s.W1}", 1)
    assert "未找到" in reply_again


async def test_test(handler: CommandHandler) -> None:
    reply = await handler("/test", 1)
    assert "链路正常" in reply


async def test_unknown(handler: CommandHandler) -> None:
    reply = await handler("/foobar", 1)
    assert "未识别" in reply


# ── 配置回写与回滚（数据生命周期）──────────────────────────────────────


async def test_add_remove_persist_config(tmp_path: Path) -> None:
    import yaml

    storage = Storage(str(tmp_path / "c.db"))
    await storage.init()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# 头\nwatchlist: []\n\n# 下段\npolling:\n  x: 1\n", encoding="utf-8"
    )
    h = CommandHandler(storage, {}, config_path=cfg)
    r = await h(f"/add {s.W2} 乙", 1)
    assert "已加入" in r
    assert s.W2 in cfg.read_text(encoding="utf-8")
    assert "# 下段" in cfg.read_text(encoding="utf-8")
    r = await h(f"/remove {s.W2}", 1)
    assert "已移出" in r
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["watchlist"] == []


async def test_add_rollback_physical_delete(tmp_path: Path, monkeypatch) -> None:
    import src.bot.commands as cm

    storage = Storage(str(tmp_path / "c.db"))
    await storage.init()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("watchlist: []\n", encoding="utf-8")
    h = CommandHandler(storage, {}, config_path=cfg)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cm, "save_watchlist_yaml", boom)
    # 新插入回滚：物理删除，无 inactive 残留
    r = await h(f"/add {s.W3} 丙", 1)
    assert "落盘失败" in r
    assert all(
        row["address"] != s.W3
        for row in await storage.list_wallets(active_only=False)
    )
    # 重新激活回滚：恢复停用（active=0）
    await storage.add_wallet(s.W4, "丁")
    await storage.remove_wallet(s.W4)
    r = await h(f"/add {s.W4} 丁回", 1)
    assert "落盘失败" in r
    w4 = next(
        x for x in await storage.list_wallets(active_only=False)
        if x["address"] == s.W4
    )
    assert w4["active"] == 0
