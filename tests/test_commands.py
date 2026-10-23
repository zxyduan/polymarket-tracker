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
