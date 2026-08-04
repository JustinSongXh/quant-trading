"""issue #35：板块成分股数据层的测试（mock HTTP，不打网络）

覆盖 issue 列的 6 项：分片×协议轮换、7 天/30 天两种 TTL、过期缓存在取数失败时回落并标
stale、名称匹配四级降级、后缀归一不等于包含匹配、响应结构异常不抛异常。另外测了页面依赖
的 `cached_status`（只读缓存判断选股入口开不开）与 `unavailable_reason` 的三种口径。

`data/sector_cons.py` 在函数内 `import requests`，所以这里用往 sys.modules 塞假模块的
方式拦请求——比 patch 一个模块级引用更贴近真实调用路径（连 URL 里的分片/协议都能断言）。
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import types
from contextlib import contextmanager

import data.sector_cons as sc

INDUSTRY_MAP_URL_FS = sc._FS_BOARDS["industry"]


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeRequests:
    """记录每次调用的 (url, params)，按 handler 决定返回什么/抛什么"""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        out = self._handler(url, dict(params or {}), len(self.calls))
        if isinstance(out, Exception):
            raise out
        return _Resp(out)


@contextmanager
def fake_requests(handler):
    fake = FakeRequests(handler)
    module = types.ModuleType("requests")
    module.get = fake.get
    old = sys.modules.get("requests")
    sys.modules["requests"] = module
    try:
        yield fake
    finally:
        if old is not None:
            sys.modules["requests"] = old
        else:
            del sys.modules["requests"]


@contextmanager
def temp_cache(alias: dict | None = None):
    """把缓存目录与 alias 表指到临时目录，绝不碰 data/cache 与仓库里的 alias 文件"""
    old_dir, old_alias, old_good = sc._CACHE_DIR, sc._ALIAS_FILE, sc._last_good
    with tempfile.TemporaryDirectory() as tmp:
        sc._CACHE_DIR = os.path.join(tmp, "sector_cons")
        os.makedirs(sc._CACHE_DIR, exist_ok=True)
        sc._ALIAS_FILE = os.path.join(tmp, "em_board_alias.json")
        if alias is not None:
            with open(sc._ALIAS_FILE, "w", encoding="utf-8") as f:
                json.dump(alias, f, ensure_ascii=False)
        sc._last_good = None      # 上次成功的组合会改变轮换顺序，测试里必须归零
        try:
            yield tmp
        finally:
            sc._CACHE_DIR, sc._ALIAS_FILE, sc._last_good = old_dir, old_alias, old_good


def _page(rows, total=None):
    return {"rc": 0, "data": {"total": total if total is not None else len(rows),
                              "diff": rows}}


def _board_rows(mapping: dict):
    return [{"f12": code, "f14": name} for name, code in mapping.items()]


def _cons_rows(n, prefix="60000"):
    return [{"f12": f"{prefix}{i:01d}"[-6:], "f14": f"股票{i}",
             "f2": 10.0 + i, "f3": float(i)} for i in range(n)]


def _write_cache(name, payload, age_days=0.0):
    path = os.path.join(sc._CACHE_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


# ========== 1. 分片 × 协议轮换 ==========

def test_shard_rotation_third_combo_succeeds():
    """前两个 (分片,协议) 组合当场断连，第三个成功 → 拿到数据"""
    def handler(url, params, n):
        if n < 3:
            return ConnectionError("RemoteDisconnected")
        return _page(_board_rows({"消费电子": "BK1036"}))

    with temp_cache(), fake_requests(handler) as fake:
        got = sc.get_em_board_map("industry", attempts=1)
        last_good = sc._last_good     # temp_cache 退出时会还原，得在里面看
    assert got == {"消费电子": "BK1036"}
    assert len(fake.calls) == 3
    # 轮换顺序里协议也在轮：第 1、2 次是同一分片的 http/https
    first, second = fake.calls[0][0], fake.calls[1][0]
    assert first.startswith("http://") and second.startswith("https://")
    assert last_good is not None      # 成功的组合被记住，下次先试它


def test_all_combos_fail_returns_none():
    def handler(url, params, n):
        return ConnectionError("RemoteDisconnected")

    with temp_cache(), fake_requests(handler) as fake:
        assert sc.get_em_board_map("industry", attempts=1) is None
    assert len(fake.calls) == len(sc._COMBOS)      # 22 个组合全试过才放弃


def test_last_good_combo_is_tried_first():
    with temp_cache():
        sc._last_good = (99, "https")
        order = sc._combo_order()
    assert order[0] == (99, "https")
    assert len(order) == len(sc._COMBOS) and len(set(order)) == len(sc._COMBOS)


# ========== 2. 翻页（pz 硬上限 100）==========

def test_paging_follows_total_and_partial_failure_discards_batch():
    """第 1 页给 total=150 → 要取 2 页；第 2 页失败 → 整批 None（半截名录会让匹配莫名失败）"""
    def handler(url, params, n):
        if params["pn"] == 1:
            return _page(_board_rows({f"板块{i}": f"BK{i:04d}" for i in range(100)}),
                         total=150)
        return ConnectionError("RemoteDisconnected")

    with temp_cache(), fake_requests(handler):
        assert sc.get_em_board_map("industry", attempts=1) is None

    def handler_ok(url, params, n):
        if params["pn"] == 1:
            return _page(_board_rows({f"板块{i}": f"BK{i:04d}" for i in range(100)}),
                         total=150)
        return _page(_board_rows({f"板块{i}": f"BK{i:04d}" for i in range(100, 150)}),
                     total=150)

    with temp_cache(), fake_requests(handler_ok):
        got = sc.get_em_board_map("industry", attempts=1)
    assert got is not None and len(got) == 150
    assert got["板块149"] == "BK0149"


# ========== 3. 两种 TTL 各自生效 ==========

def test_board_map_cache_is_fresh_for_30_days():
    """10 天前的名录缓存仍算新鲜（30 天 TTL）→ 一个请求都不发"""
    def handler(url, params, n):
        raise AssertionError("名录缓存还新鲜，不该打接口")

    with temp_cache():
        _write_cache("board_map_industry.json", {"消费电子": "BK1036"}, age_days=10)
        with fake_requests(handler) as fake:
            got = sc.get_em_board_map("industry")
        assert got == {"消费电子": "BK1036"}
        assert fake.calls == []


def test_cons_cache_7d_ttl():
    """成分股缓存 3 天前 → 直接用；10 天前 → 过期，重新取数"""
    with temp_cache():
        _write_cache("board_map_industry.json", {"消费电子": "BK1036"}, age_days=1)
        _write_cache("cons_BK1036.json",
                     [{"code": "000001", "name": "老股", "price": 1.0, "change_pct": 0.0}],
                     age_days=3)

        def handler(url, params, n):
            raise AssertionError("3 天前的成分股缓存还新鲜，不该打接口")

        with fake_requests(handler):
            got = sc.get_sector_constituents("消费电子", "industry")
        assert got["stale"] is False
        assert [c["code"] for c in got["constituents"]] == ["000001"]
        assert got["em_code"] == "BK1036" and got["match_level"] == sc.MATCH_EXACT

        # 同一份缓存改成 10 天前 → 超过 7 天 TTL，走取数
        _write_cache("cons_BK1036.json",
                     [{"code": "000001", "name": "老股", "price": 1.0, "change_pct": 0.0}],
                     age_days=10)
        with fake_requests(lambda u, p, n: _page(_cons_rows(3))) as fake:
            got = sc.get_sector_constituents("消费电子", "industry")
        assert fake.calls, "缓存已过期，应该打接口"
        assert len(got["constituents"]) == 3 and got["stale"] is False
        assert "fs" in fake.calls[0][1] and fake.calls[0][1]["fs"] == "b:BK1036"


# ========== 4. 过期缓存回落 ==========

def test_stale_cache_used_when_fetch_fails():
    """缓存过期 + 取数失败 → 返回过期缓存、stale=True、带缓存日期（过期的远好过没有）"""
    with temp_cache():
        _write_cache("board_map_industry.json", {"消费电子": "BK1036"}, age_days=1)
        path = _write_cache("cons_BK1036.json",
                            [{"code": "000001", "name": "老股", "price": 1.0,
                              "change_pct": 0.0}], age_days=30)
        with fake_requests(lambda u, p, n: ConnectionError("限流")):
            got = sc.get_sector_constituents("消费电子", "industry")
        assert got is not None and got["stale"] is True
        assert got["fetched_at"] == sc._cache_date(path)
        assert [c["code"] for c in got["constituents"]] == ["000001"]


def test_no_cache_and_fetch_fails_returns_none():
    with temp_cache():
        _write_cache("board_map_industry.json", {"消费电子": "BK1036"}, age_days=1)
        with fake_requests(lambda u, p, n: ConnectionError("限流")):
            assert sc.get_sector_constituents("消费电子", "industry") is None


# ========== 5. 名称匹配四级降级 ==========

BOARD_MAP = {"消费电子": "BK1036", "锂电池": "BK1004", "储能概念": "BK1013",
             "新能源车": "BK0493", "电池": "BK1015"}


def test_match_level_1_exact():
    with temp_cache():
        got = sc.match_em_board("消费电子", BOARD_MAP)
    assert got == {"em_code": "BK1036", "em_name": "消费电子",
                   "match_level": sc.MATCH_EXACT}


def test_match_level_2_suffix_both_directions():
    """去后缀（THS 锂电池概念 → 东财 锂电池）与加后缀（THS 储能 → 东财 储能概念）都要过"""
    with temp_cache():
        stripped = sc.match_em_board("锂电池概念", BOARD_MAP)
        added = sc.match_em_board("储能", BOARD_MAP)
    assert stripped == {"em_code": "BK1004", "em_name": "锂电池",
                        "match_level": sc.MATCH_STRIPPED}
    assert added == {"em_code": "BK1013", "em_name": "储能概念",
                     "match_level": sc.MATCH_STRIPPED}


def test_match_level_2_is_not_substring_match():
    """`电池` 在名录里存在且是 `锂电池概念` 的子串，但那是两个不同板块：

    - THS `锂电池概念` 必须匹配到 `锂电池`（BK1004），**不能**匹配到 `电池`（BK1015）
    - THS `碳化硅` 这种既不精确、去/加后缀也不中的，必须返回 None，不许挑个像的
    """
    with temp_cache():
        assert sc.match_em_board("锂电池概念", BOARD_MAP)["em_code"] == "BK1004"
        assert sc.match_em_board("碳化硅", BOARD_MAP) is None
        # 反向：名录里只有长名时，短的 THS 名不许靠包含关系命中
        assert sc.match_em_board("锂", {"锂电池": "BK1004"}) is None


def test_match_level_3_alias_name_and_bk_code():
    with temp_cache(alias={"_readme": ["注释键不参与匹配"],
                           "新能源汽车": "新能源车", "某小众板块": "BK9999"}):
        by_name = sc.match_em_board("新能源汽车", BOARD_MAP)
        by_code = sc.match_em_board("某小众板块", BOARD_MAP)
        # 注释键（_ 开头）不该被当成映射
        assert sc.match_em_board("_readme", BOARD_MAP) is None
    assert by_name == {"em_code": "BK0493", "em_name": "新能源车",
                       "match_level": sc.MATCH_ALIAS}
    assert by_code == {"em_code": "BK9999", "em_name": "某小众板块",
                       "match_level": sc.MATCH_ALIAS}


def test_match_level_4_failure_returns_none_not_arbitrary_board():
    """四级全失败 → None，且成分股取数**根本不发请求**（不能拿别的板块顶上）"""
    with temp_cache(alias={"xx": "不存在的东财板块"}):
        _write_cache("board_map_industry.json", BOARD_MAP, age_days=1)

        def handler(url, params, n):
            raise AssertionError("没匹配上板块就不该去取成分股")

        with fake_requests(handler) as fake:
            assert sc.get_sector_constituents("碳化硅", "industry") is None
            assert fake.calls == []
        reason = sc.unavailable_reason("碳化硅", "industry")
    assert "em_board_alias.json" in reason and "碳化硅" in reason


def test_suggestions_are_hints_only_never_used_for_matching():
    """候选名只进提示文案：`电池` 会被列为 `锂电池`/`电池` 的候选，但匹配仍然返回 None"""
    with temp_cache():
        hints = sc.suggest_em_names("锂电池", BOARD_MAP)
        assert "锂电池" in hints
        assert sc.suggest_em_names("完全不沾边", BOARD_MAP) == []
        # 匹配逻辑不受候选影响
        assert sc.match_em_board("锂", {"锂电池": "BK1004"}) is None
        _write_cache("board_map_industry.json", BOARD_MAP, age_days=1)
        reason = sc.unavailable_reason("锂电池板块概念", "industry")
    assert "人工确认" in reason and "锂电池" in reason


def test_alias_pointing_to_missing_board_does_not_fall_through():
    with temp_cache(alias={"碳化硅": "根本没有这个板块"}):
        assert sc.match_em_board("碳化硅", BOARD_MAP) is None


# ========== 6. 响应结构异常 ==========

def test_null_data_is_treated_as_failure_not_exception():
    """限流时会出现 HTTP 200 + data=null（实测）→ 当失败处理，轮完所有组合返回 None"""
    with temp_cache(), fake_requests(lambda u, p, n: {"rc": 0, "data": None}) as fake:
        assert sc.get_em_board_map("industry", attempts=1) is None
    assert len(fake.calls) == len(sc._COMBOS)

    for payload in ({}, {"data": {}}, {"data": {"diff": []}}, {"data": {"diff": None}}):
        with temp_cache(), fake_requests(lambda u, p, n, pl=payload: pl):
            assert sc.get_em_board_map("industry", attempts=1) is None


def test_bad_json_body_is_treated_as_failure():
    class _BadResp:
        def json(self):
            raise ValueError("Expecting value: line 1 column 1")

    module = types.ModuleType("requests")
    module.get = lambda *a, **k: _BadResp()
    old = sys.modules.get("requests")
    sys.modules["requests"] = module
    try:
        with temp_cache():
            assert sc.get_em_board_map("industry", attempts=1) is None
    finally:
        sys.modules["requests"] = old


def test_missing_price_fields_become_none():
    """停牌/限流时 f2/f3 会是 '-'，统一成 None 交给上层（不能当 0）"""
    rows = [{"f12": "000001", "f14": "平安银行", "f2": "-", "f3": "-"}]
    with temp_cache():
        _write_cache("board_map_industry.json", {"消费电子": "BK1036"}, age_days=1)
        with fake_requests(lambda u, p, n: _page(rows)):
            got = sc.get_sector_constituents("消费电子", "industry")
    assert got["constituents"] == [{"code": "000001", "name": "平安银行",
                                    "price": None, "change_pct": None}]


def test_invalid_kind_raises():
    try:
        sc.get_em_board_map("行业")
    except ValueError as e:
        assert "industry" in str(e)
    else:
        raise AssertionError("非法 kind 应该直接 ValueError")


# ========== 页面依赖：只读缓存的入口状态 ==========

def test_cached_status_four_states_without_network():
    def handler(url, params, n):
        raise AssertionError("cached_status 只读缓存，不该打接口")

    with temp_cache(), fake_requests(handler):
        # 名录都还没有
        assert sc.cached_status("消费电子", "industry")["state"] == sc.STATE_NO_MAP

        _write_cache("board_map_industry.json", BOARD_MAP, age_days=1)
        # 名字匹配不上 → 唯一该禁用按钮的状态
        unmatched = sc.cached_status("碳化硅", "industry")
        assert unmatched["state"] == sc.STATE_UNMATCHED
        assert "em_board_alias.json" in unmatched["reason"]

        # 匹配上了但没有成分股缓存 → 允许点击现场取
        no_cache = sc.cached_status("消费电子", "industry")
        assert no_cache["state"] == sc.STATE_NO_CACHE
        assert no_cache["em_code"] == "BK1036"

        _write_cache("cons_BK1036.json", [{"code": "000001"}, {"code": "000002"}],
                     age_days=3)
        cached = sc.cached_status("消费电子", "industry")
        assert cached["state"] == sc.STATE_CACHED
        assert cached["count"] == 2 and cached["stale"] is False and cached["fetched_at"]

        _write_cache("cons_BK1036.json", [{"code": "000001"}], age_days=10)
        assert sc.cached_status("消费电子", "industry")["stale"] is True


def test_unavailable_reason_distinguishes_three_causes():
    with temp_cache():
        assert "名录" in sc.unavailable_reason("消费电子", "industry")
        _write_cache("board_map_industry.json", BOARD_MAP, age_days=1)
        assert "em_board_alias.json" in sc.unavailable_reason("碳化硅", "industry")
        matched_reason = sc.unavailable_reason("消费电子", "industry")
        assert "BK1036" in matched_reason and "限流" in matched_reason


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"[OK] {name}")
    print(f"\n全部 {len(tests)} 项测试通过!")
