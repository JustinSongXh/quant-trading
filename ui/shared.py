"""跨页共用的展示助手（issue #42 从 app.py 拆出）

判定标准：**被两个以上页面用到**才放这里；只服务一个页面的助手留在那个页面模块里。
"""

import streamlit as st
import pandas as pd
from data.kline import get_kline


# ========== 工具函数 ==========

def get_stock_data(code, days=365, force_refresh=False, stock_type=None):
    """页面用的日K入口；实现在 `data/kline.py`（#41 起四处副本合并到数据层）。

    保留这个薄封装是因为页面代码到处在用这个名字与参数顺序，改名属于第三阶段拆
    app.py 的事。
    """
    return get_kline(code, days=days, stock_type=stock_type,
                     force_refresh=force_refresh)


def market_label(stype, market):
    """类型/市场 → 概览分组标签：板块 / 指数 / 港股 / A股"""
    if stype == "sector":
        return "板块"
    if stype == "index":
        return "指数"
    return "港股" if market == "HK" else "A股"


def get_recommendation(decision):
    """根据决策值返回推荐文案和颜色"""
    if decision == 1:
        return "买入", "#e74c3c"
    elif decision == -1:
        return "卖出", "#27ae60"
    return "观望", "#999999"



SIGNAL_SOURCE_LABELS = {"technical": "技术指标", "chanlun": "缠论", "kronos": "Kronos 模型"}


def signed_color(v):
    """正红负绿（沿用全站涨跌配色）；缺失值灰"""
    if v is None or pd.isna(v):
        return "color:#999999"
    if v > 0:
        return "color:#e74c3c"
    if v < 0:
        return "color:#27ae60"
    return ""



def render_strategy_params(params: list[dict], key_prefix: str) -> dict:
    """按策略 PARAMS 元数据动态渲染参数输入框（主面板），返回 {param_key: value}

    完全数据驱动：新策略只要在自己的 PARAMS 里声明参数即可，无需改这里。
    """
    kwargs = {}
    if not params:
        return kwargs
    cols = st.columns(min(len(params), 3))
    for i, p in enumerate(params):
        col = cols[i % len(cols)]
        wkey = f"{key_prefix}_{p['key']}"
        if p.get("is_pct"):
            # 百分比参数：界面用百分数显示，回传时转回小数
            val = col.number_input(
                f"{p['label']}（%）",
                min_value=float(p["min"]) * 100, max_value=float(p["max"]) * 100,
                value=st.session_state.get(wkey, float(p["default"]) * 100),
                step=float(p["step"]) * 100, key=wkey,
            )
            kwargs[p["key"]] = round(val / 100, 4)
        else:
            val = col.number_input(
                p["label"],
                min_value=float(p["min"]), max_value=float(p["max"]),
                value=st.session_state.get(wkey, float(p["default"])),
                step=float(p["step"]), key=wkey,
            )
            kwargs[p["key"]] = val
    return kwargs

