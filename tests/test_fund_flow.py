"""issue #34：板块资金流数据层的测试（mock akshare，不打网络）

覆盖 issue 列的 6 项：两种列结构归一化成同一 schema、概念侧列名叫 `行业` 的回归、
非法档位报错、两种 TTL 各自生效且不同 (kind, period) 不串味、取数失败返回 None、
快照落盘主键去重。
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date

import pandas as pd

import data.cache as cache
import data.fund_flow as ff
import data.realtime as realtime

# 实测列结构（值取自 2026-08-04 行业侧第一行）
LIVE_COLUMNS = ["序号", "行业", "行业指数", "行业-涨跌幅", "流入资金", "流出资金",
                "净额", "公司家数", "领涨股", "领涨股-涨跌幅", "当前价"]
# N日排行少了 领涨股/当前价，涨跌幅那列改名叫 阶段涨跌幅 且是带 % 的字符串
RANK_COLUMNS = ["序号", "行业", "公司家数", "行业指数", "阶段涨跌幅",
                "流入资金", "流出资金", "净额"]


def _live_df(names=("元件", "半导体")):
    rows = [{
        "序号": i + 1, "行业": n, "行业指数": 20127.5 + i, "行业-涨跌幅": 4.41 - i,
        "流入资金": 160.93 - i, "流出资金": 120.1, "净额": 40.84 - i,
        "公司家数": 63, "领涨股": "嘉立创", "领涨股-涨跌幅": 147.49, "当前价": 209.03,
    } for i, n in enumerate(names)]
    return pd.DataFrame(rows, columns=LIVE_COLUMNS)


def _rank_df(names=("教育", "半导体")):
    rows = [{
        "序号": i + 1, "行业": n, "公司家数": 15, "行业指数": 4085.61 + i,
        # 字符串带百分号——不解析的话页面按字典序排
        "阶段涨跌幅": f"{13.67 - i:.2f}%",
        "流入资金": 11.09, "流出资金": 8.17, "净额": 2.92 - i,
    } for i, n in enumerate(names)]
    return pd.DataFrame(rows, columns=RANK_COLUMNS)


def _install_fake_ak(calls=None, exc=None, empty=False, concept_names=("CRO概念", "储能")):
    """替掉 data.fund_flow 里 lazy import 的 akshare"""
    class _FakeAk:
        @staticmethod
        def stock_fund_flow_industry(symbol):
            if calls is not None:
                calls.append(("industry", symbol))
            if exc:
                raise exc
            if empty:
                return pd.DataFrame()
            return _live_df() if symbol == ff.LIVE_PERIOD else _rank_df()

        @staticmethod
        def stock_fund_flow_concept(symbol):
            if calls is not None:
                calls.append(("concept", symbol))
            if exc:
                raise exc
            if empty:
                return pd.DataFrame()
            # 概念侧板块名列实测也叫 `行业`
            return (_live_df(concept_names) if symbol == ff.LIVE_PERIOD
                    else _rank_df(concept_names))

    sys.modules["akshare"] = _FakeAk()
    return calls


def _clear_cache():
    with ff._lock:
        ff._cache.clear()


# ---------- 1. 两种列结构 → 同一 schema ----------
def test_same_schema_for_live_and_rank():
    _install_fake_ak()
    _clear_cache()
    live = ff.sector_fund_flow_ranking("industry", ff.LIVE_PERIOD)
    rank = ff.sector_fund_flow_ranking("industry", "20日")
    assert set(live[0]) == set(rank[0]) == set(ff.SCHEMA_UNITS), set(live[0]) ^ set(rank[0])

    # 即时档：裸 float 的 行业-涨跌幅
    assert live[0]["sector"] == "元件" and live[0]["change_pct"] == 4.41
    assert live[0]["net"] == 40.84 and live[0]["company_count"] == 63
    assert live[0]["leader"] == "嘉立创" and live[0]["leader_change_pct"] == 147.49
    # N日档：'13.67%' 字符串要解析成 13.67，且没有领涨股字段
    assert rank[0]["change_pct"] == 13.67, rank[0]
    assert rank[0]["leader"] is None and rank[0]["leader_change_pct"] is None
    assert rank[0]["net"] == 2.92
    # 序号 / 当前价 之类不进 schema
    assert "序号" not in live[0] and "当前价" not in live[0]
    print("[OK] 即时 与 N日排行 两种列结构归一化成同一 schema（阶段涨跌幅的 % 被解析）")


def test_amount_refuses_unit_suffix():
    """资金列单位固定亿元，出现 万/亿 后缀说明口径变了 → None，不静默放大 1e8"""
    assert ff._amount(40.84) == 40.84
    assert ff._amount("40.84") == 40.84
    assert ff._amount("40.84亿") is None
    for raw in (None, False, "", "--"):
        assert ff._amount(raw) is None, raw
    print("[OK] 资金列不做单位换算：带后缀 → None 而不是放大 1e8")


# ---------- 2. 概念侧列名叫 `行业`（回归） ----------
def test_concept_uses_industry_column_name():
    _install_fake_ak()
    _clear_cache()
    rows = ff.sector_fund_flow_ranking("concept", "20日")
    assert rows and rows[0]["sector"] == "CRO概念", rows[:1]
    assert ff._COL_SECTOR == "行业", "概念侧板块名列实测就叫 行业，按 概念 取会 KeyError"
    live = ff.sector_fund_flow_ranking("concept", ff.LIVE_PERIOD)
    assert [r["sector"] for r in live] == ["CRO概念", "储能"]
    print("[OK] 概念侧板块名列叫 `行业` 也能正确解析")


# ---------- 3. 非法档位 / kind ----------
def test_invalid_period_raises_with_all_five():
    _install_fake_ak(calls=[])
    for bad in ("45日", "一个月", "2026-07-15~2026-08-01", "", None):
        try:
            ff.sector_fund_flow_ranking("industry", bad)
            raise AssertionError(f"{bad!r} 应当被拒绝")
        except ValueError as e:
            msg = str(e)
            for p in ff.VALID_PERIODS:
                assert p in msg, f"错误信息要列出五个合法档位，缺 {p}: {msg}"
    try:
        ff.sector_fund_flow_ranking("hk_industry", "20日")
        raise AssertionError("非法 kind 应当被拒绝")
    except ValueError as e:
        assert "industry" in str(e)
    print("[OK] 非法档位/kind 直接 ValueError，且错误信息列出五个合法档位")


# ---------- 4. 两种 TTL 各自生效，(kind, period) 互不串味 ----------
def test_two_ttls_and_no_cross_talk():
    calls = _install_fake_ak(calls=[])
    _clear_cache()
    assert ff._ttl_for(ff.LIVE_PERIOD) == 60
    assert ff._ttl_for("20日") == 30 * 60

    ff.sector_fund_flow_ranking("industry", ff.LIVE_PERIOD)
    ff.sector_fund_flow_ranking("industry", ff.LIVE_PERIOD)
    assert calls == [("industry", "即时")], f"60s 内该命中缓存，实际 {calls}"

    # 换 period / 换 kind 都是独立的缓存项，不能串味
    ff.sector_fund_flow_ranking("industry", "20日")
    ff.sector_fund_flow_ranking("concept", ff.LIVE_PERIOD)
    assert calls == [("industry", "即时"), ("industry", "20日排行"), ("concept", "即时")], calls
    assert len(ff._cache) == 3

    # 即时档 61 秒前的缓存过期 → 重取；N日档同样时刻仍然新鲜（30min）
    def _age(key, secs):
        ts, val = ff._cache[key]
        ff._cache[key] = (ts - secs, val)

    _age(("industry", ff.LIVE_PERIOD), 61)
    _age(("industry", "20日"), 61)
    ff.sector_fund_flow_ranking("industry", ff.LIVE_PERIOD)
    ff.sector_fund_flow_ranking("industry", "20日")
    assert calls[-1] == ("industry", "即时"), f"即时档应过期重取，实际 {calls}"
    assert calls.count(("industry", "20日排行")) == 1, f"N日档 61s 仍新鲜，实际 {calls}"

    _age(("industry", "20日"), 30 * 60 + 1)
    ff.sector_fund_flow_ranking("industry", "20日")
    assert calls.count(("industry", "20日排行")) == 2, f"N日档超 30min 应重取，实际 {calls}"

    # force_refresh 绕过缓存
    n = len(calls)
    ff.sector_fund_flow_ranking("industry", ff.LIVE_PERIOD, force_refresh=True)
    assert len(calls) == n + 1
    print("[OK] 即时 60s / 排行 30min 两种 TTL 各自生效，(kind, period) 互不串味")


# ---------- 5. 取数失败 → None ----------
def test_fetch_failure_returns_none():
    _clear_cache()
    _install_fake_ak(exc=RuntimeError("10jqka 限流"))
    assert ff.sector_fund_flow_ranking("industry", "20日") is None
    assert ff._cache == {}, "失败不该进缓存，否则 30 分钟内都拿不到数据"

    _install_fake_ak(empty=True)
    assert ff.sector_fund_flow_ranking("concept", ff.LIVE_PERIOD) is None

    # 表在但缺板块名列（接口改版）也算失败
    class _BadAk:
        @staticmethod
        def stock_fund_flow_industry(symbol):
            return pd.DataFrame([{"概念": "半导体", "净额": 1.0}])
    sys.modules["akshare"] = _BadAk()
    assert ff.sector_fund_flow_ranking("industry", ff.LIVE_PERIOD) is None
    print("[OK] 接口抛异常 / 空表 / 缺列 → None，不抛给页面")


def test_duplicate_sector_names_dedup_keeps_first():
    """接口分页期间行情在动，跨页边界的板块会重复出现（实测 建筑材料 两条、点位/涨跌不同）。

    不去重的话 net_by_sector 建 dict 是「后者胜」、快照表主键冲突是「前者胜」，
    同一份数据两条路给出不同的值。
    """
    dup_df = pd.DataFrame([
        {"序号": 1, "行业": "建筑材料", "行业指数": 5309.25, "行业-涨跌幅": 0.5,
         "流入资金": 30.0, "流出资金": 22.0, "净额": 8.03, "公司家数": 73,
         "领涨股": "志特新材", "领涨股-涨跌幅": 5.1, "当前价": 12.3},
        {"序号": 2, "行业": "水泥", "行业指数": 1000.0, "行业-涨跌幅": 1.0,
         "流入资金": 5.0, "流出资金": 4.0, "净额": 1.0, "公司家数": 20,
         "领涨股": "海螺水泥", "领涨股-涨跌幅": 2.0, "当前价": 25.0},
        # 同一个板块的另一个瞬间（点位/涨跌/净额都略有不同）
        {"序号": 3, "行业": "建筑材料", "行业指数": 5305.02, "行业-涨跌幅": 0.42,
         "流入资金": 29.5, "流出资金": 21.5, "净额": 7.97, "公司家数": 73,
         "领涨股": "志特新材", "领涨股-涨跌幅": 5.0, "当前价": 12.28},
    ], columns=LIVE_COLUMNS)

    class _DupAk:
        @staticmethod
        def stock_fund_flow_industry(symbol):
            return dup_df

    sys.modules["akshare"] = _DupAk()
    _clear_cache()
    rows = ff.sector_fund_flow_ranking("industry", ff.LIVE_PERIOD)
    assert [r["sector"] for r in rows] == ["建筑材料", "水泥"], rows
    assert rows[0]["net"] == 8.03, "保留首次出现的那条"
    assert ff.net_by_sector("industry", ff.LIVE_PERIOD)["建筑材料"] == 8.03
    print("[OK] 同名板块去重保留首次出现（分页期间行情在动会重复返回）")


def test_net_by_sector():
    _install_fake_ak()
    _clear_cache()
    nets = ff.net_by_sector("industry", "20日")
    assert nets == {"教育": 2.92, "半导体": 1.92}, nets

    _clear_cache()
    _install_fake_ak(exc=RuntimeError("boom"))
    assert ff.net_by_sector("industry", "20日") == {}
    print("[OK] net_by_sector 给 {板块名: 净额}，取数失败给空 dict")


# ---------- 6. 快照落盘：主键去重 ----------
def _with_temp_db(fn):
    """把 DuckDB 指到临时库，别污染 data/cache/market.duckdb"""
    tmp = tempfile.mkdtemp(prefix="ff_test_")
    original = cache.CACHE_DB_PATH
    cache.CACHE_DB_PATH = os.path.join(tmp, "market.duckdb")
    try:
        return fn()
    finally:
        cache.CACHE_DB_PATH = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_snapshot_dedup_on_primary_key():
    def body():
        calls = _install_fake_ak(calls=[])
        _clear_cache()
        traded = realtime.market_traded_today
        realtime.market_traded_today = lambda d: True    # 交易日闸门放行
        try:
            day = date(2026, 8, 3)
            first = ff.snapshot_fund_flow(day)
            # 2 kind × 2 档（即时 + 20日）× 2 行
            assert first == 8, first
            assert ff.snapshot_fund_flow(day) == 0, "同一天重跑不该产生重复行"

            df = cache.load_fund_flow_snapshot()
            assert len(df) == 8, len(df)
            assert set(df["kind"]) == {"industry", "concept"}
            assert set(df["period"]) == set(ff.SNAPSHOT_PERIODS)
            # rank 存接口原始顺序，资金单位原样（亿元）
            row = df[(df["kind"] == "industry") & (df["period"] == "20日")
                     & (df["sector"] == "教育")].iloc[0]
            assert row["rank"] == 1 and float(row["net"]) == 2.92
            assert float(row["change_pct"]) == 13.67

            # 换一天照常写入
            assert ff.snapshot_fund_flow(date(2026, 8, 4)) == 8
            assert len(cache.load_fund_flow_snapshot(kind="industry", period="20日")) == 4
        finally:
            realtime.market_traded_today = traded
    _with_temp_db(body)
    print("[OK] 快照落盘：(date, kind, period, sector) 重复写入不产生重复行")


def test_snapshot_skips_non_trading_day():
    def body():
        _install_fake_ak(calls=[])
        _clear_cache()
        traded = realtime.market_traded_today
        realtime.market_traded_today = lambda d: False   # 非交易日
        try:
            assert ff.snapshot_fund_flow(date(2026, 8, 1)) == 0
            assert cache.load_fund_flow_snapshot().empty, \
                "非交易日 THS 给的是上一交易日数据，按今天写进去就是重复行"
        finally:
            realtime.market_traded_today = traded
    _with_temp_db(body)
    print("[OK] 非交易日跳过落盘（闸门同板块 bar 那套）")


def test_snapshot_partial_failure_keeps_going():
    """某一档取数失败时其余档照落，不整批放弃"""
    def body():
        class _FlakyAk:
            @staticmethod
            def stock_fund_flow_industry(symbol):
                if symbol == ff.LIVE_PERIOD:
                    raise RuntimeError("限流")
                return _rank_df()

            @staticmethod
            def stock_fund_flow_concept(symbol):
                return _live_df(("CRO概念", "储能")) if symbol == ff.LIVE_PERIOD \
                    else _rank_df(("CRO概念", "储能"))

        sys.modules["akshare"] = _FlakyAk()
        _clear_cache()
        traded = realtime.market_traded_today
        realtime.market_traded_today = lambda d: True
        try:
            assert ff.snapshot_fund_flow(date(2026, 8, 3)) == 6  # 8 - 失败那档的 2 行
        finally:
            realtime.market_traded_today = traded
    _with_temp_db(body)
    print("[OK] 单档取数失败不影响其余档落盘")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
