"""issue #37：股票池批量增删 + 板块两页合一的测试（不打网络）

`_STOCKS_FILE` 指到临时文件，不碰真实 config/stocks.json。页面级用 streamlit.testing 的
AppTest 断言路由（6 页、含「板块分析」、旧 session 值映射）。
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config.settings as cfg
from config.settings import (
    add_stock, add_stocks, get_sector_info, load_stock_pool, remove_stock,
    remove_stocks,
)

APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


def _temp_pool(fn):
    """把股票池文件指到临时目录跑 fn，跑完还原"""
    tmp = tempfile.mkdtemp(prefix="pool_test_")
    original = cfg._STOCKS_FILE
    cfg._STOCKS_FILE = os.path.join(tmp, "stocks.json")
    with open(cfg._STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump([{"code": "600519", "name": "贵州茅台", "market": "A"}], f)
    try:
        return fn()
    finally:
        cfg._STOCKS_FILE = original
        shutil.rmtree(tmp, ignore_errors=True)


def _sectors(n, kind="industry", start=881100):
    return [{"code": str(start + i), "name": f"板块{i}", "market": "A",
             "type": "sector", "sector_kind": kind} for i in range(n)]


# ---------- 1. 批量添加：条数 / 判重 / 幂等 ----------
def test_add_stocks_counts_skips_and_idempotent():
    def body():
        assert add_stocks(_sectors(90)) == 90
        assert len(load_stock_pool()) == 91          # 原有 1 只个股
        assert add_stocks(_sectors(90)) == 0, "重复批量添加应当幂等"
        # 部分重叠：只加新的那 10 个
        assert add_stocks(_sectors(100)) == 10
        # entries 自身重复也只进一条
        dup = _sectors(1, start=999999) * 5
        assert add_stocks(dup) == 1
        assert add_stocks([]) == 0
    _temp_pool(body)
    print("[OK] add_stocks 批量新增计数正确，已存在/自身重复都跳过，幂等")


# ---------- 2. sector_kind 与 (code, type) 并存 ----------
def test_add_stocks_keeps_kind_and_type_pairs():
    def body():
        assert add_stocks([
            {"code": "881121", "name": "半导体", "type": "sector", "sector_kind": "industry"},
            {"code": "300008", "name": "新能源汽车", "type": "sector", "sector_kind": "concept"},
            # 同一个 code 既是个股也是指数：按 (code, type) 判重，两条都要在
            {"code": "000001", "name": "平安银行"},
            {"code": "000001", "name": "上证指数", "type": "index"},
        ]) == 4
        assert get_sector_info("881121") == ("半导体", "industry")
        assert get_sector_info("300008") == ("新能源汽车", "concept")
        pool = load_stock_pool()
        types = {(s["code"], s.get("type", "stock")) for s in pool}
        assert ("000001", "stock") in types and ("000001", "index") in types
        # 个股不写 type 字段（沿用原有 json 形状）
        stock = next(s for s in pool if s["code"] == "000001" and "type" not in s)
        assert stock["name"] == "平安银行"
        # 缺 sector_kind 的板块回落 industry
        assert add_stocks([{"code": "881999", "name": "某板块", "type": "sector"}]) == 1
        assert get_sector_info("881999") == ("某板块", "industry")
    _temp_pool(body)
    print("[OK] 批量添加保留 sector_kind，(code, type) 不同可并存，个股不写 type")


# ---------- 3. 批量只写一次文件 ----------
def test_add_stocks_writes_file_once():
    def body():
        calls = []
        original_save = cfg.save_stock_pool
        cfg.save_stock_pool = lambda pool: (calls.append(len(pool)), original_save(pool))[1]
        try:
            assert add_stocks(_sectors(90)) == 90
        finally:
            cfg.save_stock_pool = original_save
        assert len(calls) == 1, f"90 个板块该只写一次 json，实际写了 {len(calls)} 次"
        assert calls[0] == 91
    _temp_pool(body)
    print("[OK] add_stocks 批量只写一次文件（不是 N 次 load/save 的语法糖）")


# ---------- 4. 批量移除 ----------
def test_remove_stocks_bulk():
    def body():
        add_stocks(_sectors(30, kind="industry"))
        add_stocks(_sectors(10, kind="concept", start=300000))
        assert len(load_stock_pool()) == 41

        # 只移除 concept 那批
        concepts = [(s["code"], "sector") for s in load_stock_pool()
                    if s.get("sector_kind") == "concept"]
        assert remove_stocks(concepts) == 10
        assert len(load_stock_pool()) == 31

        # 删不存在的项不报错，返回实际删掉的数量
        assert remove_stocks([("999999", "sector"), ("881100", "sector")]) == 1
        assert remove_stocks([]) == 0
        # type 不匹配不会误删（881101 是 sector，不是 stock）
        assert remove_stocks([("881101", "stock")]) == 0
        assert any(s["code"] == "881101" for s in load_stock_pool())
    _temp_pool(body)
    print("[OK] remove_stocks 按 (code, type) 批量删，删不存在的不报错、不误删")


# ---------- 5. 单个接口行为不变（回归） ----------
def test_single_add_remove_unchanged():
    def body():
        assert add_stock("881121", "半导体", "A", stock_type="sector",
                         sector_kind="industry") is True
        assert add_stock("881121", "半导体", "A", stock_type="sector",
                         sector_kind="industry") is False, "已存在应返回 False"
        assert get_sector_info("881121") == ("半导体", "industry")
        remove_stock("881121", stock_type="sector")
        assert get_sector_info("881121") is None
        assert add_stock("000300", "沪深300", "A", stock_type="index") is True
        assert any(s["code"] == "000300" and s.get("type") == "index"
                   for s in load_stock_pool())
    _temp_pool(body)
    print("[OK] add_stock / remove_stock 单个接口行为不变")


# ---------- 6. 页面级：两页合一 + 旧 session 映射 ----------
def test_pages_merged_and_legacy_session_mapped():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(APP, default_timeout=900)
    at.session_state["page"] = "板块相关性"      # #37 之前的旧值
    at.run()
    assert not at.exception, at.exception
    page_radio = at.sidebar.radio[0]
    assert list(page_radio.options) == ["全局信号总览", "单股票分析", "新闻列表",
                                        "股票池管理", "板块分析", "自定义策略"], page_radio.options
    assert page_radio.value == "板块分析", "旧的「板块相关性」应映射到合并后的页"
    assert [t.value for t in at.title] == ["板块分析"]

    # 视图切换：相关性 / 轮动 用同一份板块选择，各渲染一次都不该抛
    view = next(r for r in at.radio if r.label == "视图")
    assert list(view.options) == ["相关性", "轮动"], view.options
    assert view.value == "相关性"
    at2 = view.set_value("轮动").run()
    assert not at2.exception, at2.exception
    assert len(at2.tabs) == 4, "轮动视图 4 个 tab"
    print("[OK] 侧边栏 6 页含「板块分析」，旧 session 值映射，两个视图各渲染无异常")


def test_no_stale_page_names_left():
    src = open(APP, encoding="utf-8").read()
    for gone in ('pages = ["全局信号总览", "单股票分析", "新闻列表", "股票池管理", "板块相关性"',
                 "page_sector_correlation()", "page_sector_rotation()"):
        assert gone not in src, f"合页后不该再有 {gone}"
    assert "def page_sector_analysis" in src
    assert "_render_sector_correlation" in src and "_render_sector_rotation" in src
    # 那个从未生效过的入口（池外板块会被静默丢弃）已删除
    assert "从全量板块加入本次分析" not in src
    print("[OK] 旧的两个页面入口已清干净，池外板块那个坏入口已删")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
