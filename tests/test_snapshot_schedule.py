"""快照推送时间表解析测试。"""

from src.main import _parse_snapshot_schedule


def test_parse_basic_schedule() -> None:
    out = _parse_snapshot_schedule(["06:00", "09:30", "24:00"])
    assert out == [(6, 0, "06:00"), (9, 30, "09:30"), (0, 0, "24:00")]


def test_parse_skips_bad_items() -> None:
    out = _parse_snapshot_schedule(["08:00", "abc", "25:00"])
    # "25:00" 不做语义拦截，只校验可解析，hour 原样传入（由调度自然不命中）
    assert out == [(8, 0, "08:00"), (25, 0, "25:00")]


def test_parse_dedupes_midnight_variants() -> None:
    # 00:00 与 24:00 同时存在，只保留一条，防午夜双推
    out = _parse_snapshot_schedule(["24:00", "00:00"])
    assert out == [(0, 0, "24:00")]


def test_parse_dedupes_repeated_entries() -> None:
    out = _parse_snapshot_schedule(["08:00", "08:00", "08:00"])
    assert out == [(8, 0, "08:00")]
