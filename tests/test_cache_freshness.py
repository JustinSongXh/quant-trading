"""issue #26：收盘后当日 EOD 未发布导致缓存死抓 的修复测试（确定性，monkeypatch）"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, date, timedelta
import data.realtime as rt
import data.cache as cache
from data.fetcher import _today_bar_from_realtime

COLS = ["open", "close", "high", "low", "volume"]


def test_today_bar_filled_when_snapshot_is_today():
    """实时快照日期==今日 → 补出一根当日 bar（OHLCV 来自现价）"""
    rt.get_realtime_quotes = lambda items: {
        (items[0][0], items[0][1]): {
            "price": 100.0, "open": 99.0, "high": 101.0, "low": 98.0,
            "volume": 1234.0, "date": date.today().isoformat(),
        }
    }
    bar = _today_bar_from_realtime("002594", "stock", date.today(), COLS)
    assert bar is not None and len(bar) == 1
    assert bar.index[0].date() == date.today()
    assert bar["close"].iloc[0] == 100.0 and bar["high"].iloc[0] == 101.0
    assert list(bar.columns) == COLS
    print("[OK] 当日快照 → 补出当日 bar")


def test_no_fill_when_snapshot_is_stale():
    """非交易日：腾讯返回上一交易日快照（日期≠今日）→ 绝不补成今日 bar"""
    yest = (date.today() - timedelta(days=1)).isoformat()
    rt.get_realtime_quotes = lambda items: {
        (items[0][0], items[0][1]): {"price": 100.0, "date": yest}
    }
    assert _today_bar_from_realtime("002594", "stock", date.today(), COLS) is None
    print("[OK] 隔日快照 → 不补（日期校验生效）")


def test_no_fill_when_quote_unavailable():
    """实时接口取不到 → 不补 bar（交给 is_cache_fresh 宽限兜底）"""
    rt.get_realtime_quotes = lambda items: {}
    assert _today_bar_from_realtime("002594", "stock", date.today(), COLS) is None
    print("[OK] 取价失败 → 不补")


def test_is_cache_fresh_grace_stops_refetch():
    """兜底宽限：今天已抓过（updated_at 为今日）即视为新鲜，即便 last_bar 很旧"""
    orig = cache._load_meta
    try:
        cache._load_meta = lambda sym: {
            "last_bar_date": "2000-01-01", "updated_at": datetime.now().isoformat()}
        assert cache.is_cache_fresh("ANY") is True, "今天抓过应判新鲜，避免死抓"

        cache._load_meta = lambda sym: {
            "last_bar_date": "2000-01-01", "updated_at": "2000-01-01T10:00:00"}
        assert cache.is_cache_fresh("ANY") is False, "陈旧且非今日抓取应判过期"
    finally:
        cache._load_meta = orig
    print("[OK] is_cache_fresh 宽限：今日抓过止抓，旧数据仍过期")


if __name__ == "__main__":
    test_today_bar_filled_when_snapshot_is_today()
    test_no_fill_when_snapshot_is_stale()
    test_no_fill_when_quote_unavailable()
    test_is_cache_fresh_grace_stops_refetch()
    print("\n全部测试通过!")
