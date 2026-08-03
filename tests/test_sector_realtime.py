"""issue #32：板块盘中涨跌幅滞后一交易日 的修复测试（确定性，monkeypatch，不打网络）

三件事一起收：
1. 板块盘中走 THS 实时快照，不再用「日K最后两根收盘价」——后者在盘中算出来的是
   上一交易日的涨跌（实测 2026-08-03 13:54 消费电子显示 +3.49%，那是 07-31 的
   涨幅，当日实为 +0.10%）。
2. 表头「昨收(MM-DD)」的日期跟着**昨收**那根 bar 走，板块此前跟的是现价那根。
3. 板块代码不再进腾讯——概念板块的 3xxxxx 段与创业板股票号相撞（300008 → sz300008
   是天海防务，300733 → 西菱动力），腾讯照样返回并以 sector 的 key 存进字典。
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta

import pandas as pd

import data.realtime as rt


def _kline(rows):
    """rows: [(YYYY-MM-DD, close)] → 带 DatetimeIndex 的 df"""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows], name="date")
    return pd.DataFrame({"close": [c for _, c in rows]}, index=idx)


# 实测数据：日K 07-30→07-31 是 +3.49%，而 07-31→当日快照是 +0.10%
CONSUMER_ELEC = _kline([("2026-07-30", 10536.302), ("2026-07-31", 10903.572)])
SNAPSHOT = {
    "name": "消费电子", "price": 10914.47, "prev_close": 10903.57,
    "change_pct": 0.10, "pe": None, "date": None,
    "open": 10905.0, "high": 10950.0, "low": 10900.0, "volume": 1234,
}


# ---------- 1. 板块不进腾讯 ----------
def test_code_to_tencent_rejects_sector():
    """板块代码拼成 sz300008 会撞上天海防务，必须当场炸而不是静默返回"""
    try:
        rt._code_to_tencent("300008", "sector")
    except ValueError as e:
        assert "300008" in str(e)
        print(f"[OK] _code_to_tencent 拒绝板块：{e}")
        return
    raise AssertionError("板块代码竟然拼出了腾讯代码，会静默返回别的股票的行情")


def test_mixed_list_sends_no_sector_to_tencent():
    """混合列表：板块走 THS，只有个股/指数进腾讯的 query"""
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        raise RuntimeError("断网，只看 query")

    orig_get, orig_snap = rt.requests.get, rt.get_sector_snapshot
    orig_traded = rt.market_traded_today
    try:
        rt.requests.get = fake_get
        rt.get_sector_snapshot = lambda name, kind: dict(SNAPSHOT)
        rt.market_traded_today = lambda d: True
        out = rt.get_realtime_quotes([
            ("300008", "sector"), ("600000", "stock"), ("000001", "index"),
        ])
    finally:
        rt.requests.get, rt.get_sector_snapshot = orig_get, orig_snap
        rt.market_traded_today = orig_traded

    assert "300008" not in seen["url"], f"板块代码进了腾讯 query：{seen['url']}"
    assert "sh600000" in seen["url"] and "sh000001" in seen["url"], seen["url"]
    # 腾讯那边炸了也不该连累已取到的板块
    assert ("300008", "sector") in out, "腾讯请求失败把板块快照也丢了"
    assert out[("300008", "sector")]["pe"] is None, "板块不该有 PE"
    print(f"[OK] 板块不进腾讯 query，且腾讯失败不连累板块：{seen['url']}")


def test_sector_quote_date_follows_trading_gate():
    """板块快照没有日期字段，date 填的是交易日闸门的结果"""
    orig_snap, orig_traded = rt.get_sector_snapshot, rt.market_traded_today
    try:
        rt.get_sector_snapshot = lambda name, kind: dict(SNAPSHOT)

        rt.market_traded_today = lambda d: True
        q = rt.get_realtime_quotes([("300008", "sector")])[("300008", "sector")]
        assert q["date"] == date.today().isoformat(), q["date"]

        rt.market_traded_today = lambda d: False
        q = rt.get_realtime_quotes([("300008", "sector")])[("300008", "sector")]
        assert q["date"] is None, f"未开盘却填了日期：{q['date']}"
    finally:
        rt.get_sector_snapshot, rt.market_traded_today = orig_snap, orig_traded
    print("[OK] 板块 date 跟随交易日闸门（开盘→今日 / 未开盘→None）")


# ---------- 2. 快照解析与 TTL 缓存 ----------
def test_snapshot_parsed_from_ths():
    """昨收 × (1+板块涨幅) = 现价，成交量(万手) → 手"""
    rt._sector_cache.clear()
    calls = []

    class FakeAk:
        @staticmethod
        def stock_board_industry_info_ths(symbol):
            calls.append(symbol)
            return pd.DataFrame({
                "项目": ["今开", "昨收", "最高", "最低", "成交量(万手)", "板块涨幅"],
                "值": ["10905.00", "10903.57", "10950.00", "10900.00", "595.52", "0.10%"],
            })

    sys.modules["akshare"] = FakeAk
    try:
        snap = rt.get_sector_snapshot("消费电子", "industry")
    finally:
        del sys.modules["akshare"]
        rt._sector_cache.clear()

    assert snap["prev_close"] == 10903.57
    assert snap["change_pct"] == 0.10
    assert snap["price"] == round(10903.57 * 1.001, 3), snap["price"]
    assert snap["volume"] == 5955200, snap["volume"]
    print(f"[OK] THS 快照解析：昨收 {snap['prev_close']} → 现价 {snap['price']}")


def test_snapshot_ttl_cache():
    """同一 (name, kind) 连续两次只打一次 akshare——板块只能逐个查，总览页反复刷不能每次重打"""
    rt._sector_cache.clear()
    calls = []

    class FakeAk:
        @staticmethod
        def stock_board_concept_info_ths(symbol):
            calls.append(symbol)
            return pd.DataFrame({
                "项目": ["今开", "昨收", "最高", "最低", "成交量(万手)", "板块涨幅"],
                "值": ["4139.67", "4139.48", "4200.00", "4130.00", "100.0", "1.05%"],
            })

    sys.modules["akshare"] = FakeAk
    try:
        a = rt.get_sector_snapshot("新能源汽车", "concept")
        b = rt.get_sector_snapshot("新能源汽车", "concept")
        rt.get_sector_snapshot("锂电池概念", "concept")   # 不同 key 不该命中
    finally:
        del sys.modules["akshare"]
        rt._sector_cache.clear()

    assert a == b
    assert calls == ["新能源汽车", "锂电池概念"], f"缓存没生效：{calls}"
    print(f"[OK] 快照 TTL 缓存：3 次调用只打 2 次接口（{calls}）")


def test_snapshot_failure_returns_none():
    """接口炸了 / 字段缺失 → None，让调用方回落，不抛给页面"""
    rt._sector_cache.clear()

    class BoomAk:
        @staticmethod
        def stock_board_industry_info_ths(symbol):
            raise RuntimeError("index out of bounds")

    class MissingFieldAk:
        @staticmethod
        def stock_board_industry_info_ths(symbol):
            return pd.DataFrame({"项目": ["今开"], "值": ["1.0"]})

    for fake, label in ((BoomAk, "接口抛异常"), (MissingFieldAk, "字段缺失")):
        rt._sector_cache.clear()
        sys.modules["akshare"] = fake
        try:
            assert rt.get_sector_snapshot("消费电子", "industry") is None, label
        finally:
            del sys.modules["akshare"]
    rt._sector_cache.clear()
    print("[OK] 快照失败/缺字段 → None")


# ---------- 3. 展示层：板块与个股同一口径 ----------
def _meta(code, stype, df, rt_quote):
    from app import _overview_meta_row
    return _overview_meta_row(code, "消费电子", stype, "A", df, rt_quote)


def test_sector_uses_snapshot_not_kline():
    """盘中：日K两根算出 +3.49%（上一交易日），快照说 +0.10%（当日）→ 取快照"""
    row = _meta("881124", "sector", CONSUMER_ELEC, SNAPSHOT)
    assert row["涨跌幅"] == 0.10, f"仍在用日K口径：{row['涨跌幅']}"
    assert row["现价"] == 10914.47
    assert row["昨收"] == 10903.57, "昨收该用快照自带的那个，与现价同源"
    print(f"[OK] 板块盘中用快照：{row['昨收']} → {row['现价']}（{row['涨跌幅']:+.2f}%）")


def test_sector_falls_back_to_kline():
    """快照取不到 → 回落日K最后两根，即改动前的行为，不会更差"""
    row = _meta("881124", "sector", CONSUMER_ELEC, None)
    assert row["现价"] == 10903.57 and row["昨收"] == 10536.30
    assert row["涨跌幅"] == 3.49, row["涨跌幅"]
    assert row["日期"] == "2026-07-30", f"日期该跟着昨收那根：{row['日期']}"
    print(f"[OK] 快照缺失回落日K：{row['涨跌幅']:+.2f}%（日期 {row['日期']}）")


def test_date_column_follows_prev_close():
    """「日期」= 昨收那根 bar 的日期，板块与个股同一条规则"""
    today, yest = date.today(), date.today() - timedelta(days=1)
    older = today - timedelta(days=2)

    # 盘中：日K 已剔除当日 bar，最后一根就是昨收
    intraday = _kline([(str(older), 100.0), (str(yest), 110.0)])
    # 收盘后：当日 bar 已补上，昨收要往前一根
    after = _kline([(str(yest), 110.0), (str(today), 121.0)])

    quote = {"price": 121.0, "pe": None}
    for stype in ("sector", "stock"):
        q = dict(quote, prev_close=110.0) if stype == "sector" else dict(quote)

        row = _meta("X", stype, intraday, q)
        assert row["日期"] == str(yest), f"{stype} 盘中日期错：{row['日期']}"
        assert row["昨收"] == 110.0, f"{stype} 盘中昨收错：{row['昨收']}"

        row = _meta("X", stype, after, q)
        assert row["日期"] == str(yest), f"{stype} 收盘后日期错：{row['日期']}"
        assert row["昨收"] == 110.0, f"{stype} 收盘后昨收错：{row['昨收']}"
    print("[OK] 「日期」跟着昨收那根走，板块与个股一致")


def test_sector_row_needs_no_leftover_rt():
    """板块行不再依赖上一轮循环残留的 rt——排在股票池第一位也不该 UnboundLocalError

    改动前 rt 只在个股分支赋值，而 pe_raw = rt.get("pe") 在 if/else 之后无条件执行。
    """
    row = _meta("881124", "sector", CONSUMER_ELEC, None)
    assert row["市盈率"] == "-", row["市盈率"]
    row = _meta("881124", "sector", CONSUMER_ELEC, SNAPSHOT)
    assert row["市盈率"] == "-", "板块不该显示 PE"
    print("[OK] 板块行独立成立（rt 为 None 也不炸），PE 恒为 -")


def test_stock_behaviour_unchanged():
    """个股口径不受影响：有实时价用实时价，取不到则现价=昨收、涨跌幅 0"""
    df = _kline([("2026-07-30", 6.20), ("2026-07-31", 6.24)])
    row = _meta("600000", "stock", df, {"price": 6.33, "pe": 31.94})
    assert row["现价"] == 6.33 and row["昨收"] == 6.24
    assert row["涨跌幅"] == round((6.33 - 6.24) / 6.24 * 100, 2)
    assert row["市盈率"] == 31.94

    row = _meta("600000", "stock", df, None)
    assert row["现价"] == row["昨收"] == 6.24 and row["涨跌幅"] == 0.0
    print("[OK] 个股口径不变（含取价失败时 现价==昨收、涨跌幅 0）")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
