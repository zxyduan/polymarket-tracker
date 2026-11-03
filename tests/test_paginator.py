"""MessagePaginator 测试（§7.2 边界 4095/4096/4097）。"""

from framework.models.message import RenderedMessage
from framework.render.paginator import LengthPolicy, MessagePaginator


def test_under_limit_single_page():
    p = MessagePaginator()
    msg = RenderedMessage(body="line1\nline2", body_format="text",
                          title="日报", event_keys=("daily_report",))
    out = p.paginate(msg)
    assert len(out) == 1
    assert out[0].event_keys == ("daily_report",)
    assert out[0].body_format == "text"


def test_boundary_4095_4096_single_4097_splits():
    p = MessagePaginator(LengthPolicy(max_chars=4096, max_bytes=4096))
    under = "x" * 4095
    assert len(p.paginate(RenderedMessage(body=under))) == 1

    at = "x" * 4096
    assert len(p.paginate(RenderedMessage(body=at))) == 1

    over = "x" * 4097 + "\nline2"
    out = p.paginate(RenderedMessage(body=over))
    assert len(out) >= 2


def test_continuation_title():
    p = MessagePaginator(LengthPolicy(max_chars=20))
    body = "\n".join(["a" * 15, "b" * 15, "c" * 15])
    out = p.paginate(RenderedMessage(body=body, title="日报"))
    assert out[0].body.startswith("日报\n")
    assert any("（续" in o.body for o in out)
