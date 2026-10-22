"""推送通道（notify）测试：ntfy 发布语义 + 多平台工厂构建 + 平台配置读取。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from src.notify.factory import build_notifiers
from src.notify.ntfy import NtfyNotifier
from src.notify.telegram import TelegramNotifier
from src.settings import Settings


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )


class FakeClient:
    """捕获 POST 请求（url/content/headers），不真正外发。"""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self._status = status

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return

    async def post(
        self, url: str, content: str, headers: dict[str, str]
    ) -> FakeResponse:
        self.calls.append((url, content, headers))
        return FakeResponse(self._status)


class FakeTelegramApi:
    """最小 TelegramApi 替身（仅验证通道是否被构建）。"""

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeAuth:
    def chats(self) -> list[int]:
        return [1]


def _settings_from_raw(tmp_path: Path, raw: dict[str, Any]) -> Settings:
    """直接构造 Settings（绕过文件 IO），注入原始 YAML dict。"""
    p = tmp_path / "config.yaml"
    return Settings(
        bot_token="",
        allowed_chat_ids=[],
        log_level="INFO",
        config_path=p,
        raw=raw,
    )


async def test_ntfy_send_posts_text(monkeypatch: Any, tmp_path: Path) -> None:
    """ntfy 发布：POST {server}/{topic}，body 为消息文本，可带鉴权/标题头。"""
    import src.notify.ntfy as ntfy_mod

    fake = FakeClient()
    monkeypatch.setattr(ntfy_mod.httpx, "AsyncClient", lambda **_: fake)

    n = NtfyNotifier(
        "https://ntfy.sh/",
        "poly-test",
        access_token="tok123",
        title_prefix="Polymarket",
        priority="4",
        tags="chart, robot",
    )
    await n.send("<b>hello</b>", event_keys=["k1"])
    assert len(fake.calls) == 1
    url, content, headers = fake.calls[0]
    assert url == "https://ntfy.sh/poly-test"
    assert content == "<b>hello</b>"
    assert headers["Authorization"] == "Bearer tok123"
    assert headers["X-Title"] == "Polymarket"
    assert headers["X-Priority"] == "4"
    assert headers["X-Tags"] == "chart,robot"
    assert "poly-test" in n.describe()


async def test_ntfy_send_http_error_raises(monkeypatch: Any) -> None:
    """发布失败（4xx/5xx）必须抛异常，由 sender 层降级，不静默吞掉。"""
    import src.notify.ntfy as ntfy_mod

    fake = FakeClient(status=401)
    monkeypatch.setattr(ntfy_mod.httpx, "AsyncClient", lambda **_: fake)
    n = NtfyNotifier("https://ntfy.sh", "poly-test")
    with pytest.raises(httpx.HTTPStatusError):
        await n.send("msg")


def test_factory_ntfy_snapshot_uses_snapshot_topic(tmp_path: Path) -> None:
    """旧写法 platform=ntfy：交易员用 topic、快照优先 snapshot_topic。"""
    raw = {
        "notify": {"platform": "ntfy"},
        "ntfy": {
            "server": "https://ntfy.example.com",
            "topic": "alerts",
            "snapshot_topic": "snap-topic",
        },
    }
    settings = _settings_from_raw(tmp_path, raw)
    trader = build_notifiers(settings, None, None, None, kind="trader")
    snap = build_notifiers(settings, None, None, None, kind="snapshot")
    assert len(trader) == 1 and isinstance(trader[0], NtfyNotifier)
    assert len(snap) == 1 and isinstance(snap[0], NtfyNotifier)
    assert "ntfy.example.com/alerts" in trader[0].describe()
    assert "ntfy.example.com/snap-topic" in snap[0].describe()


def test_factory_multi_platform_both_built(tmp_path: Path) -> None:
    """新写法 platforms=[ntfy, telegram]：两个通道同时构建（一起发送）。"""
    raw = {
        "notify": {"platforms": ["ntfy", "telegram"]},
        "ntfy": {"server": "https://ntfy.sh", "topic": "alerts"},
    }
    settings = _settings_from_raw(tmp_path, raw)
    notifiers = build_notifiers(
        settings, None, FakeTelegramApi(), FakeAuth(), kind="trader"
    )
    assert len(notifiers) == 2
    assert isinstance(notifiers[0], NtfyNotifier)
    assert isinstance(notifiers[1], TelegramNotifier)


def test_factory_multi_telegram_without_token_skipped(tmp_path: Path) -> None:
    """platforms 含 telegram 但无 token：telegram 被跳过，ntfy 保留。"""
    raw = {
        "notify": {"platforms": ["ntfy", "telegram"]},
        "ntfy": {"server": "https://ntfy.sh", "topic": "alerts"},
    }
    settings = _settings_from_raw(tmp_path, raw)
    notifiers = build_notifiers(settings, None, None, None, kind="trader")
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], NtfyNotifier)


def test_factory_ntfy_missing_topic_returns_empty(tmp_path: Path) -> None:
    raw = {"notify": {"platform": "ntfy"}, "ntfy": {"server": "https://ntfy.sh"}}
    settings = _settings_from_raw(tmp_path, raw)
    assert build_notifiers(settings, None, None, None, kind="trader") == []


def test_factory_telegram_requires_token(tmp_path: Path) -> None:
    """平台缺省（telegram）但无 token：返回空列表（仅采集不推送）。"""
    settings = _settings_from_raw(tmp_path, {})
    assert build_notifiers(settings, None, None, None, kind="trader") == []


def test_platform_reading(tmp_path: Path) -> None:
    """notify.platforms 列表读取；旧写法 platform 兼容；缺省 telegram。"""
    settings = _settings_from_raw(tmp_path, {})
    assert settings.notify_platforms == ["telegram"]
    assert settings.notify_platform == "telegram"

    settings2 = _settings_from_raw(tmp_path, {"notify": {"platform": "NTFY"}})
    assert settings2.notify_platforms == ["ntfy"]
    assert settings2.notify_platform == "ntfy"

    settings3 = _settings_from_raw(
        tmp_path, {"notify": {"platforms": ["ntfy", "telegram"]}}
    )
    assert settings3.notify_platforms == ["ntfy", "telegram"]
