"""P4: sources 不依赖 store 实现类。"""

import ast
import pathlib


def test_p4_sources_no_store_import():
    src_dir = pathlib.Path(__file__).parent.parent / "framework" / "sources"
    for f in src_dir.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    assert "store" not in n.name, f"{f.name}: {n.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "store" not in mod, f"{f.name}: from {mod}"
