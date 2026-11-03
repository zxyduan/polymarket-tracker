"""sources 适配器薄壳与 RawRecord 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from framework.models.raw import RawRecord, RecordKind
from framework.sources.gamma_adapter import GammaAdapter
from framework.sources.rtds_adapter import RtdsAdapter


def test_recordkind_values():
    assert RecordKind.EVENT.value == "event"
    assert RecordKind.SNAPSHOT.value == "snapshot"


def test_rawrecord_frozen():
    r = RawRecord(
        source="rtds",
        kind=RecordKind.EVENT,
        received_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
        payload={"a": 1},
    )
    assert r.source == "rtds"


def test_adapters_construct():
    assert RtdsAdapter("wss://example").connected is False
    assert isinstance(GammaAdapter("https://example"), GammaAdapter)
