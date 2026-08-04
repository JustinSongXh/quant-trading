"""对话助手工具集的测试（issue #36，不调模型、不打网络）

覆盖 issue 六「工具集」那 6 项：schema 合法且枚举型参数真是 enum、非法档位在 schema 层
就传不出来、sector_pick 的 top_n 上限生效、总览扫描缺缓存时只回提示不现算、每个
description 都写了代价/边界、工具模块只读（AST 检查没有写文件/落库调用）。

**为什么盯着 schema 和 description**：模型是靠这两样选工具、传参数的。把「只有五个档位」
写在文字里模型会绕过，写进 enum 就在 schema 层传不出来——这是可测的，文字叮嘱不可测。
"""

import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analysis.chat import tools

# 说明里必须出现的关键词：能给什么 / 代价多大 / 边界在哪
COST_WORDS = ("代价", "慢", "秒", "毫秒", "缓存")
BOUND_WORDS = ("边界", "只", "不支持", "上限", "⚠️")


# ---------- 1. schema 合法 ----------
def test_every_tool_schema_is_valid():
    for spec in tools.TOOLS:
        schema = spec.input_schema
        assert schema.get("type") == "object", f"{spec.name} 的 schema 不是 object"
        props = schema.get("properties")
        assert isinstance(props, dict), f"{spec.name} 缺 properties"
        required = schema.get("required", [])
        assert isinstance(required, list), f"{spec.name} 的 required 不是 list"
        for key in required:
            assert key in props, f"{spec.name} required 里的 {key} 不在 properties 里"
        # 不许有未声明的自由参数：模型传了错名字应该当场失败，而不是被静默忽略
        assert schema.get("additionalProperties") is False, \
            f"{spec.name} 没有 additionalProperties: False"
        for key, prop in props.items():
            assert prop.get("type"), f"{spec.name}.{key} 没写 type"
        assert callable(spec.handler), f"{spec.name} 的 handler 不可调用"
        assert spec.data_class in ("A", "B", "C"), f"{spec.name} 的 data_class 非法"
    print(f"[OK] {len(tools.TOOLS)} 个工具的 input_schema 均合法")


def test_tool_names_unique_and_snake_case():
    names = [s.name for s in tools.TOOLS]
    assert len(names) == len(set(names)), f"工具名有重复：{names}"
    for n in names:
        assert n.islower() and " " not in n, f"工具名 {n} 不是 snake_case"
    assert tools.TOOL_NAMES == tuple(names)


# ---------- 2. 枚举型参数必须是 enum ----------
def _prop(name: str, key: str) -> dict:
    return tools.get_spec(name).input_schema["properties"][key]


def test_enum_params_are_enums_not_free_strings():
    """kind / period / source / windows / order 一律 enum。

    回归测试：有人把 enum 改成 `{"type": "string"}` 时这里必须红——那等于把「只有五个
    档位」的约束退回成一句文字叮嘱，模型就能传出 "45日"。
    """
    cases = [
        ("sector_fund_flow_ranking", "kind", tools.SECTOR_KINDS),
        ("sector_fund_flow_ranking", "period", tools.FLOW_PERIODS),
        ("sector_fund_flow_ranking", "order", ["inflow", "outflow"]),
        ("sector_pick", "kind", tools.SECTOR_KINDS),
        ("get_signals", "source", tools.SIGNAL_SOURCES),
    ]
    for tool_name, key, expect in cases:
        prop = _prop(tool_name, key)
        assert prop.get("enum") == expect, f"{tool_name}.{key} 的 enum 是 {prop.get('enum')}"
    windows = _prop("sector_momentum", "windows")
    assert windows["type"] == "array"
    assert windows["items"].get("enum") == tools.MOMENTUM_WINDOWS, \
        "sector_momentum.windows 的元素必须是 enum（只有 5/20/60 三档）"
    print("[OK] 枚举型参数都是 enum，不是自由字符串")


def test_illegal_fund_flow_period_cannot_be_expressed():
    """「过去 45 天」「7月15日到8月1日」这类区间在 schema 层就传不出来"""
    enum = _prop("sector_fund_flow_ranking", "period")["enum"]
    for bad in ("45日", "1个月", "2026-07-15~2026-08-01", "任意区间", "30日"):
        assert bad not in enum, f"{bad} 竟然是合法档位"
    assert enum == ["即时", "3日", "5日", "10日", "20日"]
    # description 里也要明确写出「做不到就说做不到」，否则模型会拿 20日 硬答
    desc = tools.get_spec("sector_fund_flow_ranking").description
    assert "不支持任意日期区间" in desc and "20日" in desc
    print("[OK] 资金流只有五档，非法区间无法表达")


def test_period_guard_also_holds_at_handler_level():
    """双保险：schema 之外，handler 拿到非法档位也不能抛堆栈（数据层会 ValueError）"""
    import data.fund_flow as ff

    saved = ff.sector_fund_flow_ranking
    ff.sector_fund_flow_ranking = lambda kind, period: (_ for _ in ()).throw(
        ValueError(f"非法档位 {period}，只有 即时/3日/5日/10日/20日"))
    try:
        out = tools.call("sector_fund_flow_ranking", {"kind": "industry", "period": "45日"})
    finally:
        ff.sector_fund_flow_ranking = saved
    assert out["available"] is False
    assert "非法档位" in out["hint"], out
    print("[OK] 非法档位到了 handler 也只是可转达的一句话，不是堆栈")


# ---------- 3. sector_pick 的 top_n 上限 ----------
def test_sector_pick_top_n_cap():
    prop = _prop("sector_pick", "top_n")
    assert prop["maximum"] == tools.PICK_TOP_N_MAX == 30, prop
    assert prop["minimum"] == 1

    import analysis.sector_pick as sp

    seen = {}
    saved = sp.pick
    sp.pick = lambda code, kind=None, top_n=None, **kw: (
        seen.update({"code": code, "kind": kind, "top_n": top_n}) or
        {"available": True, "sector": "光伏设备", "kind": "industry",
         "em_name": "光伏设备", "em_code": "BK1031", "total_constituents": 68,
         "computed": top_n, "top_n": top_n, "stale": False, "fetched_at": "x",
         "sector_return": -1.0, "weights": {}, "candidates": [], "warnings": []})
    try:
        # 越界值（模型硬传 999）在 handler 里被夹到 30，而不是照着算 999 只
        out = tools.call("sector_pick", {"sector_code": "881279", "top_n": 999})
        assert seen["top_n"] == 30, seen
        assert out["computed"] == 30
        tools.call("sector_pick", {"sector_code": "881279", "top_n": 5})
        assert seen["top_n"] == 5
        tools.call("sector_pick", {"sector_code": "881279"})
        assert seen["top_n"] == tools.PICK_TOP_N_DEFAULT
    finally:
        sp.pick = saved
    print("[OK] sector_pick 的 top_n 上限 30 在 schema 与 handler 两处都生效")


def test_sector_pick_transcribes_coverage():
    """C 类工具必须把覆盖面带回给模型（否则「算了 20 只」会被说成「全扫过了」）"""
    import analysis.sector_pick as sp

    saved = sp.pick
    sp.pick = lambda code, kind=None, top_n=20, **kw: {
        "available": True, "sector": "光伏设备", "kind": "industry",
        "em_name": "光伏设备", "em_code": "BK1031", "total_constituents": 68,
        "computed": 20, "top_n": 20, "stale": True, "fetched_at": "2026-07-20 03:20",
        "sector_return": -10.5, "weights": {"technical": 1.0},
        "candidates": [{"code": "300776", "name": "帝尔激光", "score": 1.0,
                        "fund_flags": [], "note": ""}],
        "warnings": ["成分股用的是过期缓存"]}
    try:
        out = tools.call("sector_pick", {"sector_code": "881279"})
    finally:
        sp.pick = saved
    assert out["total_constituents"] == 68 and out["computed"] == 20
    assert out["stale"] is True and out["fetched_at"] == "2026-07-20 03:20"
    assert "68" in out["note"] and "必须转述" in out["note"]
    print("[OK] sector_pick 把覆盖面/时效带回给模型")


def test_sector_pick_unavailable_is_a_sentence_not_an_exception():
    import analysis.sector_pick as sp

    saved = sp.pick
    sp.pick = lambda code, kind=None, top_n=20, **kw: {
        "available": False, "sector": "汽车整车",
        "warnings": ["未能匹配到东财板块：请在 config/em_board_alias.json 里加一行"]}
    try:
        out = tools.call("sector_pick", {"sector_code": "881263"})
    finally:
        sp.pick = saved
    assert out["available"] is False and "alias" in out["hint"]
    print("[OK] 板块不支持选股时给的是能照做的一句话")


# ---------- 4. 总览扫描：只读缓存 ----------
def test_overview_scan_missing_cache_returns_hint():
    """没有缓存 → available=false + hint，不抛异常、**不现算**（全池几十个标的要几十秒）"""
    out = tools.call("get_overview_scan", {})
    assert out["available"] is False, out
    assert "扫描" in out["hint"]
    # 源码自检：这个 handler 里不许出现取数/算信号的调用
    src = inspect.getsource(tools._get_overview_scan)
    for banned in ("build_signals", "fetch_daily_kline", "get_stock_data", "_kline("):
        assert banned not in src, f"get_overview_scan 里出现了 {banned}，它必须只读缓存"
    print("[OK] 总览扫描缺缓存时只回提示，不现算")


def test_overview_scan_reads_session_state():
    """有缓存时读出来并去掉 _ 前缀的内部字段"""
    import streamlit as st

    try:
        st.session_state["overview_meta"] = {
            "600519|stock": {"股票": "贵州茅台(600519)", "现价": 1358.98,
                             "涨跌幅": "+1.2%", "_code": "600519", "_type": "stock"},
        }
    except Exception:
        print("[SKIP] 无 Streamlit 脚本上下文，跳过 session_state 读取断言")
        return
    try:
        out = tools.call("get_overview_scan", {})
    finally:
        st.session_state.pop("overview_meta", None)
    assert out["available"] is True and out["returned"] == 1
    assert "_code" not in out["items"][0], "内部字段不该进模型上下文"


# ---------- 5. description 必须写清代价与边界 ----------
def test_every_description_states_cost_and_bounds():
    for spec in tools.TOOLS:
        desc = spec.description
        assert len(desc) > 40, f"{spec.name} 的 description 太短：{desc!r}"
        assert any(w in desc for w in COST_WORDS), f"{spec.name} 没写代价：{desc!r}"
        assert any(w in desc for w in BOUND_WORDS), f"{spec.name} 没写边界：{desc!r}"
    # C 类（跨标的重计算）要额外写明「慢」和覆盖面
    for spec in tools.TOOLS:
        if spec.data_class != "C":
            continue
        assert any(w in spec.description for w in ("慢", "只读缓存", "不会现算")), \
            f"C 类工具 {spec.name} 没写清代价"
    print("[OK] 每个 description 都写了能给什么/代价/边界")


def test_kronos_not_in_tool_set():
    """Kronos 不进工具集：单只 CPU 上约 40 秒（同 #35 的理由）"""
    assert "kronos" not in tools.SIGNAL_SOURCES
    assert _prop("get_signals", "source")["enum"] == ["technical", "chanlun"]
    src = inspect.getsource(tools)
    assert "kronos_pred" not in src and "generate_kronos" not in src
    print("[OK] 工具集不含 Kronos")


# ---------- 6. 工具只读 ----------
_WRITE_CALLS = ("save_kline", "save_stock_pool", "add_stock", "add_stocks",
                "remove_stock", "remove_stocks", "upsert_news_items", "promote",
                "write_text", "to_csv", "to_parquet", "unlink", "rmtree", "system",
                "open", "makedirs", "utime", "execute", "snapshot_fund_flow")


def test_tools_module_has_no_write_calls():
    """AST 自检：工具是只读的。

    用 AST 而不是字符串匹配——注释与 description 里出现「写」「保存」这类词是在说明
    约束本身，不该算命中（#30 test_no_which_call_in_source 的同一条教训）。
    """
    tree = ast.parse(inspect.getsource(tools))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        assert name not in _WRITE_CALLS, (
            f"第 {node.lineno} 行调用了 {name}()：对话工具必须是只读的——"
            f"模型不该能改股票池、落盘、删文件"
        )
    print("[OK] 工具模块没有写操作调用")


def test_tools_module_does_not_import_store_or_providers():
    """工具层不碰持久化，也不认识任何具体 provider"""
    tree = ast.parse(inspect.getsource(tools))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        assert "agent_gen" not in mod, f"工具层不该 import {mod}（那是代码生成的 store）"
        assert "chat.providers" not in mod, f"工具层不该认识具体 provider：{mod}"
    print(f"[OK] 工具层 import 干净：{sorted(set(imported))}")


# ---------- 杂项：调用包装 ----------
def test_call_catches_handler_exception():
    """handler 抛异常要变成模型能转达的一句话，而不是让整个回合以堆栈收场"""
    spec = tools.get_spec("list_pool")
    boom = tools.ToolSpec(name=spec.name, description=spec.description,
                          input_schema=spec.input_schema,
                          handler=lambda args: (_ for _ in ()).throw(RuntimeError("炸了")))
    saved = tools._BY_NAME[spec.name]
    tools._BY_NAME[spec.name] = boom
    try:
        out = tools.call("list_pool", {})
    finally:
        tools._BY_NAME[spec.name] = saved
    assert out["available"] is False and "RuntimeError: 炸了" in out["error"]
    assert "如实告知" in out["hint"]
    print("[OK] handler 异常被兜成一句话")


def test_unknown_tool_raises():
    try:
        tools.call("drop_database", {})
    except KeyError as e:
        assert "未知工具" in str(e)
    else:
        raise AssertionError("未知工具应该 KeyError")


def test_count_rows():
    assert tools.count_rows({"returned": 5, "total": 90}) == 5
    assert tools.count_rows({"count": 3, "items": [1, 2]}) == 3
    assert tools.count_rows({"items": [1, 2, 3]}) == 3
    assert tools.count_rows({"available": False, "hint": "x"}) == 1
    assert tools.count_rows("not a dict") == 1


def test_clean_drops_nan_instead_of_zero():
    """NaN → None，**不是 0**：当 0 会被读成「算出来正好是 0」"""
    assert tools._clean(float("nan")) is None
    assert tools._clean(float("inf")) is None
    assert tools._clean(1.23456789) == 1.2346
    assert tools._clean(None) is None
    assert tools._clean("x") == "x"


def test_truncation_is_explicit():
    rows = [{"i": i} for i in range(10)]
    out = tools._truncated(rows, 90)
    assert out["returned"] == 10 and out["total"] == 90
    assert "只返回了前 10 行" in out["truncated"]
    assert "truncated" not in tools._truncated(rows, 10), "没截断就不该有 truncated 字段"


def test_openai_tools_shape():
    """同一份 spec 也能转成 OpenAI 兼容声明——工具集没绑在某家 SDK 上"""
    decls = tools.openai_tools()
    assert len(decls) == len(tools.TOOLS)
    for decl, spec in zip(decls, tools.TOOLS):
        assert decl["type"] == "function"
        assert decl["function"]["name"] == spec.name
        assert decl["function"]["parameters"] == spec.input_schema
    print("[OK] 工具定义可转成 OpenAI function calling 声明")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"[OK] {name}")
    print(f"\n全部 {len(tests)} 项测试通过!")
