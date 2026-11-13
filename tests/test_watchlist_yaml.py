"""watchlist 文本级回写（save_watchlist_yaml）测试。"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.settings import save_watchlist_yaml

SAMPLE = """# 头部注释
watchlist:
  - address: "0x1111111111111111111111111111111111111111"
    label: "甲"

# 下一段注释
polling:
  x: 1
"""


def test_rewrite_preserves_other_content(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text(SAMPLE, encoding="utf-8")
    save_watchlist_yaml(
        f, [{"address": "0x2222222222222222222222222222222222222222", "label": "乙"}]
    )
    out = f.read_text(encoding="utf-8")
    assert yaml.safe_load(out)["watchlist"][0]["label"] == "乙"
    # 其余内容逐字保留
    assert "# 头部注释" in out
    assert "# 下一段注释" in out and "polling:" in out and "x: 1" in out


def test_empty_writes_flow_seq(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text(SAMPLE, encoding="utf-8")
    save_watchlist_yaml(f, [])
    out = f.read_text(encoding="utf-8")
    assert "watchlist: []" in out
    assert yaml.safe_load(out)["watchlist"] == []


def test_bak_created(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text(SAMPLE, encoding="utf-8")
    save_watchlist_yaml(f, [])
    bak = tmp_path / "config.yaml.bak"
    assert bak.exists() and bak.read_text(encoding="utf-8") == SAMPLE


def test_missing_block_inserted(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    no_block = "# 只有其他配置\npolling:\n  x: 1\n"
    f.write_text(no_block, encoding="utf-8")
    save_watchlist_yaml(
        f, [{"address": "0x3333333333333333333333333333333333333333", "label": "插"}]
    )
    out = f.read_text(encoding="utf-8")
    assert yaml.safe_load(out)["watchlist"][0]["label"] == "插"
    assert "polling:" in out and "x: 1" in out


def test_special_char_escaped(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text(SAMPLE, encoding="utf-8")
    weird = '甲"引号\\反斜杠\n换行'
    save_watchlist_yaml(
        f, [{"address": "0x4444444444444444444444444444444444444444", "label": weird}]
    )
    back = yaml.safe_load(f.read_text(encoding="utf-8"))["watchlist"][0]["label"]
    assert back == weird


def test_span_keeps_next_comment(tmp_path: Path) -> None:
    """P2 回归：段间空行与下一段前置注释不被吞掉。"""
    f = tmp_path / "config.yaml"
    f.write_text(SAMPLE, encoding="utf-8")
    save_watchlist_yaml(
        f, [{"address": "0x5555555555555555555555555555555555555555", "label": "新"}]
    )
    out = f.read_text(encoding="utf-8")
    assert "# 下一段注释" in out
    # watchlist 与下一段注释之间保留空行
    assert '\n\n# 下一段注释' in out
