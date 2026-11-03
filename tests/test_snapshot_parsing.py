"""snapshot_parsing 平移等价测试（R-06）。"""

from framework.parsing import snapshot_parsing as sp


def test_bucketize_skips_bad_points():
    out = sp.bucketize_prices([
        {"t": 3600, "p": "1.5"},
        {"t": 7200, "p": "2"},
        {"t": None, "p": 9},
        {"t": "bad", "p": 9},
    ])
    assert out[0] == [1.5]         # 3600 落到 0 号 2h 桶
    assert out[7200] == [2.0]


def test_volume_delta_floor_zero():
    assert sp.calc_volume_delta(5.0, 3.0) == 2.0
    assert sp.calc_volume_delta(3.0, 5.0) == 0.0
    assert sp.calc_volume_delta(None, 5.0) == 0.0


def test_trend_up_streak():
    hist = [{"price_close": 1.0, "trend": "up", "trend_streak": 2}]
    trend, pct, streak = sp.calc_trend(1.1, hist)
    assert trend == "up" and streak == 3 and abs(pct - 0.1) < 1e-9


def test_volatility_regime():
    hist = [{"price_close": c} for c in (1.0, 1.1, 1.0, 1.1, 1.0, 1.1)]
    short, long, regime = sp.calc_volatility(hist)
    assert short >= 0 and long >= 0


def test_parse_book_depth_top5_wall():
    data = {
        "bids": [{"price": "0.5", "size": "200"}, {"price": "0.4", "size": "1"}],
        "asks": [{"price": "0.6", "size": "1"}],
    }
    d = sp.parse_book_depth(data)
    assert d.bid_top5 == 100.4      # 0.5*200 + 0.4*1
    assert d.ask_top5 == 0.6
    assert d.bid_wall_count == 1     # 0.4*1=0.4 不到 100 墙阈值
