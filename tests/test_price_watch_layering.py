"""price_watch 分层约束测试（P4 口径）。

- `features/price_watch/**` 不 import `src` 业务层；
- `framework/sources/price_feed.py` 不 import `store` 实现类。
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).parent.parent


def _module_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Import):
        names.extend(n.name for n in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        names.append(node.module)
    return names


def test_feature_layer_does_not_import_src() -> None:
    feature_dir = _ROOT / "features" / "price_watch"
    for f in feature_dir.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            for mod in _module_names(node):
                assert not mod.startswith("src"), f"{f.name}: {mod}"


def test_price_feed_does_not_import_store() -> None:
    feed = _ROOT / "framework" / "sources" / "price_feed.py"
    tree = ast.parse(feed.read_text())
    for node in ast.walk(tree):
        for mod in _module_names(node):
            assert "store" not in mod, f"price_feed.py: {mod}"
