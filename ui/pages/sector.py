"""板块分析页：相关性 + 轮动 + 板块内选股三个视图（issue #42 从 app.py 拆出；#29/#34/#35/#37）"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

from config.pool import load_stock_pool

from ui.shared import signed_color


HEATMAP_MAX = 25
# 格子多到这个数以上就不写格内数字，只留颜色 + hover（挤成一团的数字比没有更糟）
CELL_TEXT_MAX = 15


def _corr_heatmap(mat, window, method_label, mode_label, mode, title_note=""):
    """画一张相关性热力图（矩阵已按传入顺序排好）"""
    from analysis.correlation import cluster_order

    order = cluster_order(mat)
    m = mat.loc[order, order]
    n = len(order)
    heat = dict(
        z=m.values, x=list(m.columns), y=list(m.index),
        zmin=-1, zmax=1,
        colorscale=[[0.0, "#2e8b57"], [0.5, "#f5f5f5"], [1.0, "#c0392b"]],  # 负绿 中灰 正红
        colorbar=dict(title="相关系数"),
        hovertemplate="%{y} × %{x}<br>相关系数=%{z:.3f}<extra></extra>",
    )
    if n <= CELL_TEXT_MAX:
        heat.update(text=m.round(2).values, texttemplate="%{text}",
                    textfont={"size": 10})
    fig = go.Figure(data=go.Heatmap(**heat))
    fig.update_layout(height=max(400, 34 * n + 160), xaxis=dict(side="bottom"),
                      yaxis=dict(autorange="reversed"), margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"{title_note}红=正相关（同涨跌），绿=负相关（反向）。窗口 {window} 交易日，"
               f"{method_label}，{mode_label}。"
               + ("板块 ≥3 已按层次聚类重排，抱团板块相邻。" if n >= 3 else "")
               + (f"　【{n} 个板块，格内数字已省略（只保留颜色），把鼠标停在格子上看具体数值】"
                  if n > CELL_TEXT_MAX else "")
               + ("　【原始相关被大盘主导，成长赛道普遍偏红；想看真实轮动/对冲请切「剔除大盘」】"
                  if mode == "raw" else ""))


def _render_sector_correlation(sel_codes, name_by_code):
    """板块相关性视图：热力图 + 单板块联动榜 + 相关对榜（#29，#37 合页后只收板块列表）"""
    from analysis.correlation import (
        VALID_WINDOWS, cluster_groups, cluster_order, corr_matrix, linkage_ranking,
        pairs_from_matrix,
    )

    st.caption("对一组板块的日收益率两两算相关系数：谁同涨同跌（正相关抱团）、谁反向（负相关可对冲/轮动）。")

    sel_labels = [f"{name_by_code[c]}({c})" for c in sel_codes]
    label_to_code = dict(zip(sel_labels, sel_codes))
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        window = st.radio("窗口（交易日）", VALID_WINDOWS, index=1, horizontal=True, key="corr_window")
    with c2:
        method_label = st.selectbox("方法", ["Pearson", "Spearman（抗异常）"], key="corr_method")
    with c3:
        mode_label = st.radio(
            "相关模式",
            ["原始相关", "剔除大盘（沪深300）", "剔除板块均值"],
            index=0, horizontal=True, key="corr_mode",
            help="原始相关被大盘 beta 主导（成长赛道普遍偏高）；剔除大盘/板块均值后看板块特异关系，"
                 "才能暴露真实的轮动主线和对冲对。",
        )
    method = "spearman" if method_label.startswith("Spearman") else "pearson"
    mode = {"原始相关": "raw", "剔除大盘（沪深300）": "index", "剔除板块均值": "cross"}[mode_label]

    if len(sel_codes) < 2:
        st.info("相关性至少需要 2 个板块，请在左侧多选一个。")
        return

    with st.spinner("计算中（首次取数可能较慢）…"):
        mat = corr_matrix(sel_codes, window=window, method=method, mode=mode)

    if mat.empty:
        st.warning("参与板块的收益率数据不足或对齐后无重叠交易日，换更短窗口或减少停牌/新板块再试。")
        return

    tab_heat, tab_link, tab_hedge = st.tabs(["相关性热力图", "单板块联动榜", "相关对排行"])

    with tab_heat:
        n_all = len(mat.columns)
        if n_all <= HEATMAP_MAX:
            _corr_heatmap(mat, window, method_label, mode_label, mode)
        else:
            # 板块太多：全矩阵画出来是 3390px 高、9025 个格子，读不出任何东西。
            # 改成先给「谁和谁抱团」的分组结论，再让用户挑一组画子矩阵。
            st.info(f"当前 {n_all} 个板块（> {HEATMAP_MAX}），全矩阵有 {n_all * n_all} 个格子、"
                    f"缩到屏幕宽度后字会糊成一片，所以先给**抱团分组**；要看矩阵请在下面挑一组"
                    f"或自己勾选（≤ {HEATMAP_MAX} 个）。")
            groups = cluster_groups(mat)
            st.dataframe(
                pd.DataFrame([{
                    "组": f"第 {i} 组", "成员数": len(g["members"]),
                    "组内平均相关": g["avg_corr"],
                    "成员": "、".join(g["members"]),
                } for i, g in enumerate(groups, start=1)])
                .style.format({"组内平均相关": "{:+.3f}"}, na_rep="—"),
                hide_index=True, use_container_width=True,
            )
            st.caption("按层次聚类切组（距离 = 1 − 相关系数，与热力图的重排是同一套聚类）。"
                       "「组内平均相关」越高＝抱得越紧；单成员组没有组内相关，显示「—」"
                       "（不是 0，0 会被读成不相关）。")

            labels = [f"第 {i} 组（{len(g['members'])} 个）"
                      for i, g in enumerate(groups, start=1)]
            c_group, c_pick = st.columns([1, 3])
            with c_group:
                # 默认最大的组：分组视图之后最想看的就是「最大那团内部长什么样」
                biggest = max(range(len(groups)), key=lambda i: len(groups[i]["members"]))
                chosen = st.selectbox("画哪一组的矩阵", labels, index=biggest,
                                      key="corr_group_pick")
            members = groups[labels.index(chosen)]["members"]
            with c_pick:
                custom = st.multiselect(
                    f"或自己勾选板块（≤ {HEATMAP_MAX} 个，留空则用左边选的组）",
                    list(mat.columns), default=[], key="corr_custom_pick")
            subset = custom or members
            if len(subset) > HEATMAP_MAX:
                st.warning(f"勾了 {len(subset)} 个，超过 {HEATMAP_MAX} 个就又读不清了——"
                           f"只画前 {HEATMAP_MAX} 个。")
                subset = subset[:HEATMAP_MAX]
            if len(subset) < 2:
                st.caption("这一组只有 1 个板块，画不了矩阵（相关性至少要 2 个）。"
                           "换一组，或在右边自己勾几个。")
            else:
                _corr_heatmap(mat.loc[subset, subset], window, method_label, mode_label,
                              mode, title_note=f"（{len(subset)}/{n_all} 个板块）")

    with tab_link:
        target_label = st.selectbox("选择板块", sel_labels, key="corr_link_target")
        target_code = label_to_code.get(target_label)
        rk = linkage_ranking(target_code, sel_codes, window=window, top_n=5, method=method, mode=mode)
        col_pos, col_neg = st.columns(2)
        with col_pos:
            st.markdown(f"**与「{rk['target']}」最正相关 Top5**")
            if rk["positive"]:
                st.dataframe(pd.DataFrame(rk["positive"], columns=["板块", "相关系数"])
                             .style.format({"相关系数": "{:.3f}"}), hide_index=True, use_container_width=True)
            else:
                st.caption("无数据")
        with col_neg:
            st.markdown(f"**与「{rk['target']}」最负相关 Top5**")
            if rk["negative"]:
                st.dataframe(pd.DataFrame(rk["negative"], columns=["板块", "相关系数"])
                             .style.format({"相关系数": "{:.3f}"}), hide_index=True, use_container_width=True)
            else:
                st.caption("无数据")

    with tab_hedge:
        # 两张表都吃上面已经算好的 mat：hedge_pairs() 会自己再算一遍 corr_matrix，
        # 95 个板块时等于把取数与计算白做一遍
        col_neg, col_pos = st.columns(2)
        with col_neg:
            st.markdown("**相关系数最低的板块对**（越负越可能对冲/轮动）")
            neg = pairs_from_matrix(mat, top_n=15, positive=False)
            if neg:
                st.dataframe(pd.DataFrame(neg, columns=["板块 A", "板块 B", "相关系数"])
                             .style.format({"相关系数": "{:.3f}"}),
                             hide_index=True, use_container_width=True)
            else:
                st.caption("无数据")
        with col_pos:
            st.markdown("**相关系数最高的板块对**（抱团最紧，同涨同跌）")
            pos = pairs_from_matrix(mat, top_n=15, positive=True)
            if pos:
                st.dataframe(pd.DataFrame(pos, columns=["板块 A", "板块 B", "相关系数"])
                             .style.format({"相关系数": "{:.3f}"}),
                             hide_index=True, use_container_width=True)
            else:
                st.caption("无数据")
        st.caption(f"在所选 {len(mat.columns)} 个板块的两两组合里取极值（各 15 对）。"
                   "板块多到画不了矩阵时，这两张表和「抱团分组」就是主要读法——它们与板块数无关。")


# ========== 板块轮动视图（issue #34） ==========


def _label_badge_color(label):
    from analysis.rotation import LABEL_EMERGING, LABEL_FADING, LABEL_LAGGARD, LABEL_STRONG
    return {LABEL_STRONG: "#c0392b", LABEL_EMERGING: "#e67e22",
            LABEL_FADING: "#27ae60", LABEL_LAGGARD: "#7f8c8d"}.get(label, "#7f8c8d")


def _render_sector_rotation(sel_codes, name_by_code):
    """板块轮动视图（#34，#37 合页后板块选择由 page_sector_analysis 统一给）"""
    from analysis.rotation import (
        HOTNESS_FLOW_PERIOD, HOTNESS_WINDOW, LABELS, LONG_WINDOW, SHORT_WINDOW,
        VALID_WINDOWS, col_excess, col_rank, col_ret, hotness, momentum_table,
        rotation_signals,
    )
    from data.fund_flow import LIVE_PERIOD, VALID_PERIODS, sector_fund_flow_ranking

    st.caption("板块相对强弱的排名迁移 + 资金流佐证：谁在持续走强、谁刚新晋走强、谁在转弱。"
               "全是确定性计算，不产生交易信号、不进回测。")

    # 动量/热度只算所选板块（板块日K要逐个取）；资金流排行那个 tab 是全市场，
    # 因为它一次调用就返回全部且已排序，是免费的。
    windows = st.multiselect("动量窗口（交易日）", list(VALID_WINDOWS),
                             default=list(VALID_WINDOWS), key="rot_windows")
    windows = tuple(sorted(windows))

    tab_flow, tab_mom, tab_hot, tab_sig = st.tabs(
        ["资金流排行（全市场）", "所选板块动量", "热度分排名", "轮动提示"]
    )

    # ---------- 资金流排行（全市场） ----------
    with tab_flow:
        c1, c2 = st.columns([1, 3])
        with c1:
            kind_label = st.radio("板块类型", ["行业", "概念"], horizontal=True, key="rot_kind")
        with c2:
            period = st.radio("档位", list(VALID_PERIODS), horizontal=True, key="rot_period")
        kind = "industry" if kind_label == "行业" else "concept"

        st.caption("**单位：亿元。**数据源只有 即时 / 3日 / 5日 / 10日 / 20日 这五档，"
                   "**不支持任意区间**——「过去一个月」按 20日（20 个交易日）看，"
                   "「过去 45 天」「7月15日到8月1日」这类做不到，也不能拿 20日 的数据硬答。")
        with st.spinner("取资金流排行…"):
            rows = sector_fund_flow_ranking(kind, period)
        if not rows:
            st.warning("资金流数据暂不可用（THS 接口取数失败），稍后再试。")
        else:
            live = period == LIVE_PERIOD
            table = [{
                "板块": r["sector"],
                "板块指数": r["sector_index"],
                ("当日涨跌幅%" if live else f"{period}涨跌幅%"): r["change_pct"],
                "净额(亿)": r["net"],
                "流入(亿)": r["inflow"],
                "流出(亿)": r["outflow"],
                "家数": r["company_count"],
                **({"领涨股": r["leader"], "领涨股涨跌幅%": r["leader_change_pct"]}
                   if live else {}),
            } for r in rows]
            df = pd.DataFrame(table).sort_values("净额(亿)", ascending=False,
                                                 na_position="last")
            pct_col = "当日涨跌幅%" if live else f"{period}涨跌幅%"
            signed = ["净额(亿)", pct_col] + (["领涨股涨跌幅%"] if live else [])
            fmt = {"板块指数": "{:.2f}", pct_col: "{:+.2f}", "净额(亿)": "{:+.2f}",
                   "流入(亿)": "{:.2f}", "流出(亿)": "{:.2f}"}
            if live:
                fmt["领涨股涨跌幅%"] = "{:+.2f}"
            st.dataframe(df.style.format(fmt, na_rep="—").map(signed_color, subset=signed),
                         hide_index=True, use_container_width=True, height=520)
            st.caption(f"{kind_label}板块共 {len(rows)} 个（同名去重后），按净额降序。"
                       "接口内部是分页抓的，板块条数每次会有出入，个别板块可能整批缺失。"
                       + ("N日档没有领涨股/当前价字段，接口本身不返回。" if not live else ""))

    if not sel_codes or not windows:
        with tab_mom:
            st.info("请在左侧选择至少 1 个板块，并在上方选至少 1 个动量窗口。")
        return

    with st.spinner("计算板块动量（首次取板块日K可能较慢）…"):
        mom = momentum_table(sel_codes, windows=windows)

    index_ok = bool(mom.attrs.get("index_available", True))
    excess_note = ("超额收益 = 板块累计收益 − 沪深300 同窗口累计收益。" if index_ok else
                   "⚠️ 沪深300 数据取不到，「超额」列此刻实际是**绝对收益**（已降级）。")

    # ---------- 所选板块动量矩阵 ----------
    with tab_mom:
        st.markdown(f"**行=板块，列=各窗口超额收益与排名（在所选 {len(sel_codes)} 个板块内排）**")
        show = mom.drop(columns=["code"])
        excess_cols = [col_excess(w) for w in windows]
        fmt = {}
        for w in windows:
            fmt[col_ret(w)] = "{:+.2f}"
            fmt[col_excess(w)] = "{:+.2f}"
            fmt[col_rank(w)] = "{:.0f}"
        vals = show[excess_cols].to_numpy(dtype="float64", na_value=float("nan"))
        lim = pd.Series(vals.ravel()).abs().max()
        sty = show.style.format(fmt, na_rep="—")
        if pd.notna(lim) and lim > 0:
            # 红=强绿=弱（A股习惯），以 0 为中心对称染色
            sty = sty.background_gradient(cmap="RdYlGn_r", subset=excess_cols,
                                          vmin=-float(lim), vmax=float(lim))
        st.dataframe(sty, use_container_width=True)
        st.caption(excess_note + "　排名 1 = 该窗口超额收益最高（仅在所选板块内排）。"
                   "指标为「—」的板块是日K不足该窗口（新板块/长期停牌），不参与排名。")

    # ---------- 热度分排名 ----------
    with tab_hot:
        st.markdown(f"**热度分 = w₁·z({HOTNESS_WINDOW}日超额收益) + w₂·z({HOTNESS_FLOW_PERIOD}资金净额)**")
        with st.spinner("计算热度分…"):
            hot = hotness(sel_codes)
        if hot.empty:
            st.warning("热度分算不出来（板块日K与资金流都取不到）。")
        else:
            w = hot.attrs.get("weights", {})
            st.caption(f"两项都在所选板块内做 z-score，权重 w₁={w.get('excess_20d', 1.0)}、"
                       f"w₂={w.get('net_20d', 1.0)}（改 config/settings.py::SECTOR_HOTNESS_WEIGHTS 即生效）。"
                       "「热门」写成公式而不交给模型判断：模型对「热门」的理解每次会飘，而你看不出来。")
            show = hot.drop(columns=["code"])
            ex_col, net_col = col_excess(HOTNESS_WINDOW), f"{HOTNESS_WINDOW}日资金净额"
            st.dataframe(
                show.style.format({ex_col: "{:+.2f}", net_col: "{:+.2f}",
                                   "z_超额": "{:+.2f}", "z_资金": "{:+.2f}",
                                   "热度分": "{:+.3f}"}, na_rep="—")
                    .map(signed_color, subset=[ex_col, net_col, "热度分"]),
                use_container_width=True,
            )
            missing = show[show["备注"] != ""]
            if not missing.empty:
                st.caption("⚠️ 以下板块有分项缺失，**热度分为空而不是 0**（当 0 会被排到中间，"
                           "看起来像「不冷不热」，那是假的）：" +
                           "；".join(f"{i}——{r['备注']}" for i, r in missing.iterrows()))
            plot = hot.dropna(subset=["热度分"]).sort_values("热度分")
            if not plot.empty:
                fig = go.Figure()
                for col, name, color in (("z_超额", f"z({HOTNESS_WINDOW}日超额)", "#2196F3"),
                                         ("z_资金", f"z({HOTNESS_FLOW_PERIOD}资金净额)", "#FF6F00")):
                    fig.add_trace(go.Bar(y=list(plot.index), x=plot[col], name=name,
                                         orientation="h", marker_color=color))
                fig.update_layout(barmode="relative", height=max(320, 34 * len(plot) + 140),
                                  margin=dict(l=10, r=10, t=30, b=10),
                                  xaxis_title="z 分项（叠加=热度分）",
                                  legend=dict(orientation="h", yanchor="bottom", y=1.01))
                st.plotly_chart(fig, use_container_width=True)
            _render_sector_pick_panel(hot)

    # ---------- 轮动提示 ----------
    with tab_sig:
        if SHORT_WINDOW not in windows or LONG_WINDOW not in windows:
            st.info(f"轮动标签用 {SHORT_WINDOW} 日与 {LONG_WINDOW} 日的排名迁移判定，"
                    f"请在左侧同时选上这两个窗口。")
        else:
            sigs = rotation_signals(mom, short=SHORT_WINDOW, long=LONG_WINDOW)
            st.caption(f"判定阈值写死在 analysis/rotation.py：排名迁移以「所选板块内 1/3 名次」为界"
                       f"（当前 {len(sel_codes)} 个板块 → {max(1.0, len(sel_codes) / 3):.1f} 名），"
                       f"边界达到即触发；一个板块只给一个标签（优先级 {' > '.join(LABELS)}）。"
                       + excess_note)
            if not sigs:
                st.caption("当前所选板块没有触发任何轮动标签（也可能是日K数据不足）。")
            for label in LABELS:
                group = [s for s in sigs if s["label"] == label]
                if not group:
                    continue
                color = _label_badge_color(label)
                st.markdown(
                    f'<span style="display:inline-block;background:{color};color:#fff;'
                    f'border-radius:10px;padding:2px 10px;margin:6px 6px 4px 0;'
                    f'font-size:14px">{label} · {len(group)}</span>',
                    unsafe_allow_html=True,
                )
                st.dataframe(
                    pd.DataFrame([{
                        "板块": s["sector"],
                        f"{SHORT_WINDOW}日超额%": s["excess_short"],
                        f"{LONG_WINDOW}日超额%": s["excess_long"],
                        f"{SHORT_WINDOW}日排名": s["rank_short"],
                        f"{LONG_WINDOW}日排名": s["rank_long"],
                        "名次变化": s["rank_change"],
                        "依据": s["detail"],
                    } for s in group]).style.format({
                        f"{SHORT_WINDOW}日超额%": "{:+.2f}", f"{LONG_WINDOW}日超额%": "{:+.2f}",
                        f"{SHORT_WINDOW}日排名": "{:.0f}", f"{LONG_WINDOW}日排名": "{:.0f}",
                        "名次变化": "{:+.0f}",
                    }, na_rep="—"),
                    hide_index=True, use_container_width=True,
                )


# ========== 板块内选股（issue #35：挂在热度分排名下方） ==========

_PICK_ROWS_EXPANDER_LIMIT = 20      # 板块多于这么多个就把入口列表收进 expander


def _render_sector_pick_panel(hot):
    """热度分排名下方的选股入口：一行一个板块 + 「选股」按钮（#35）

    按钮状态只看**本地缓存**（`cached_status` 不打接口）——一页十几个板块，不能为了
    画按钮去打十几次限流接口。只有「未匹配到东财板块」才禁用（那是确定性的不支持，
    等限流窗口也没用，得去补 alias）；没有缓存不等于取不到，允许点下去现场赌一枪。
    """
    from config.pool import get_sector_info
    from config.settings import SECTOR_PICK_TOP_N
    from data.sector_cons import (
        CONS_TTL_DAYS, STATE_CACHED, STATE_UNMATCHED, cached_status,
    )

    st.markdown("---")
    st.markdown("#### 板块内选股（从热门板块下钻到个股）")
    st.caption(
        "两级漏斗：**粗筛**用东财成分股自带的涨跌幅取前 N 只（免费），**细算**只对这 N 只算"
        "技术/缠论信号 + 基本面 flags + 相对板块超额收益。**不含 Kronos**（单只 CPU 上约 40 秒，"
        "20 只不可接受）。成分股是全仓库唯一用东方财富的地方，行情/资金流仍走同花顺。"
        f"成分股本地缓存 {CONS_TTL_DAYS} 天，每晚 03:20 有 job 重试刷新（接口是窗口性限流）。"
    )
    top_n = st.number_input("细算只数 N", min_value=1, max_value=100,
                            value=int(SECTOR_PICK_TOP_N), step=5, key="pick_top_n",
                            help="改大更全但更慢：每只要取日K + 算信号 + 取财报")

    rows = [(name, r["code"], r.get("热度分")) for name, r in hot.iterrows()]
    box = (st.expander(f"选择板块（{len(rows)} 个，按热度分降序）", expanded=False)
           if len(rows) > _PICK_ROWS_EXPANDER_LIMIT else st.container())
    with box:
        for name, code, score in rows:
            info = get_sector_info(code)
            kind = info[1] if info else "industry"
            status = cached_status(name, kind)
            c1, c2, c3, c4 = st.columns([2, 1, 1, 5])
            c1.markdown(f"**{name}**")
            c2.markdown("—" if score is None or pd.isna(score) else f"{score:+.2f}")
            if c3.button("选股", key=f"pick_btn_{code}",
                         disabled=status["state"] == STATE_UNMATCHED,
                         help=status["reason"] or None):
                st.session_state["pick_target"] = (code, name, kind)
            if status["state"] == STATE_CACHED:
                c4.caption(f"成分股 {status['count']} 只 · 缓存 {status['fetched_at']}"
                           + ("（**已过期**，取数失败会继续用它）" if status["stale"] else ""))
            else:
                c4.caption(status["reason"])

    target = st.session_state.get("pick_target")
    if not target:
        st.caption("点上面某个板块的「选股」按钮，这里会展开该板块的候选股排名。")
        return
    _render_sector_pick_result(target, int(top_n))


def _render_sector_pick_result(target, top_n):
    """跑 pick() 并展示候选股表（结果缓存在 session，否则每次交互都要重算一遍 #35）"""
    from analysis.sector_pick import PICK_SIGNALS, PICK_WINDOW, pick

    code, name, kind = target
    st.markdown(f"##### 「{name}」候选股")
    c1, c2 = st.columns([1, 4])
    retry = c1.button("重试取数", key="pick_retry",
                      help="只强制重取成分股（日K缓存照旧复用）。东财限流是窗口性的，"
                           "多试一轮常能撞上可用窗口")
    cache_key = (code, top_n)
    cached = st.session_state.get("pick_result")
    if retry or not cached or cached.get("key") != cache_key:
        bar = st.progress(0.0, text="取成分股…")

        def _cb(i, total, msg):
            bar.progress(i / max(1, total), text=f"细算 {i}/{total}：{msg}")

        res = pick(code, kind, top_n=top_n, sector_name=name,
                   force_cons=bool(retry), progress_cb=_cb)
        bar.empty()
        st.session_state["pick_result"] = {"key": cache_key, "res": res}
    res = st.session_state["pick_result"]["res"]

    if not res["available"]:
        st.warning((res["warnings"] or ["成分股不可用"])[0])
        return

    # 跨 kind 命中要点明（#38）：池内是行业板块、成分股却来自东财的概念板块，
    # 不说一句会像配错了
    kind_note = ("（东财把它归在"
                 f"{'概念' if res.get('em_kind') == 'concept' else '行业'}板块，"
                 "两家分类口径不一致）" if res.get("cross_kind") else "")
    st.caption(f"东财板块 {res['em_name']}({res['em_code']}){kind_note}，"
               f"名称匹配方式：{res['match_level']}。"
               f"**已从 {res['total_constituents']} 只成分股中按涨跌幅粗筛出 "
               f"{res['top_n']} 只、实际算成 {res['computed']} 只**"
               f"（改上方 N 可扩大范围；不是「全都算过了」）。"
               f"成分股快照时间 {res['fetched_at']}"
               + ("（**过期缓存**）" if res["stale"] else "")
               + f"，粗筛用的涨跌幅就是那一刻的值。")
    if res["stale"]:
        st.warning(f"成分股是过期缓存（{res['fetched_at']}）：成分调整是月度级，通常无妨，"
                   f"但「涨跌幅」这一列不是今天的，粗筛口径也随之是那天的。")

    if not res["candidates"]:
        st.warning("候选集是空的（粗筛出来的股票日K全都取不到）。" +
                   ("原因：" + "；".join(res["warnings"]) if res["warnings"] else ""))
        return

    w = res["weights"]
    st.markdown(f"**排序分 = {w['technical']}·z(技术信号) + {w['excess']}·z({PICK_WINDOW}日相对板块超额)"
                f" + {w['fundamental']}·基本面加减分**")
    st.caption(f"两个 z 分项在**候选集内**算（不是全市场）；基本面加减分是 flags 里每个正向"
               f"标签 +0.5、负向 −0.5（映射表写死在 analysis/sector_pick.py）。"
               f"细算的信号源：{'、'.join(PICK_SIGNALS)}——**不含 Kronos**。"
               f"缠论信号照算照显示但不进排序分（issue 定的公式只有三项）。"
               f"本页不产生交易信号、不进 fusion、不影响回测与仓位。")

    ret_col, ex_col = f"{PICK_WINDOW}日收益%", f"{PICK_WINDOW}日相对板块超额%"
    table = pd.DataFrame([{
        "代码": c["code"], "名称": c["name"], "现价": c["price"],
        "涨跌幅%(快照)": c["change_pct"],
        "技术信号": c["technical"], "缠论信号": c["chanlun"],
        ret_col: c["stock_return"], ex_col: c["excess"],
        "基本面加减分": c["fund_score"],
        "z_技术": c["z_technical"], "z_超额": c["z_excess"], "排序分": c["score"],
        "基本面标签": "、".join(c["fund_flags"]) if c["fund_flags"] else
                    ("（无标签）" if c["fund_flags"] == [] else "—"),
        "备注": c["note"],
    } for c in res["candidates"]])
    signed = ["涨跌幅%(快照)", "技术信号", "缠论信号", ret_col, ex_col,
              "基本面加减分", "z_技术", "z_超额", "排序分"]
    st.dataframe(
        table.style.format({
            "现价": "{:.2f}", "涨跌幅%(快照)": "{:+.2f}",
            "技术信号": "{:+.3f}", "缠论信号": "{:+.3f}",
            ret_col: "{:+.2f}", ex_col: "{:+.2f}", "基本面加减分": "{:+.1f}",
            "z_技术": "{:+.2f}", "z_超额": "{:+.2f}", "排序分": "{:+.3f}",
        }, na_rep="—").map(signed_color, subset=signed),
        hide_index=True, use_container_width=True,
    )
    st.caption(f"板块指数同期收益：{res['sector_return']:+.2f}%" if res["sector_return"] is not None
               else "板块指数日K取不到，超额收益列缺失（该项按 0 计入排序分）。")
    if res["warnings"]:
        st.caption("⚠️ " + "；".join(res["warnings"]))
    notes = table[table["备注"] != ""]
    if not notes.empty:
        st.caption("以下标的有缺项（该项按 0 计入排序分，**不是**算出来等于 0）：" +
                   "；".join(f"{r['名称']}——{r['备注']}" for _, r in notes.iterrows()))


# ========== 板块分析页（issue #37：合并相关性与轮动） ==========

SECTOR_VIEWS = ("相关性", "轮动")


def page_sector_analysis():
    """板块分析：相关性与轮动两个视图共用一份板块选择（#37）

    两页合一的收益就在这里——板块只选一次，切视图不用重选；侧边栏也从 7 项回到 6 项。
    """
    st.title("板块分析")

    pool_sectors = [s for s in load_stock_pool() if s.get("type") == "sector"]
    if not pool_sectors:
        st.info("股票池里还没有板块。请到「股票池管理」页添加行业/概念板块（支持一键批量添加）后再来分析。")
        return

    name_by_code = {s["code"]: s["name"] for s in pool_sectors}
    labels = [f"{n}({c})" for c, n in name_by_code.items()]
    label_to_code = {f"{n}({c})": c for c, n in name_by_code.items()}

    st.sidebar.markdown("**参与分析的板块**")
    sel_labels = st.sidebar.multiselect(f"池内板块（共 {len(labels)} 个）", labels,
                                        default=labels, key="sec_sectors")
    sel_codes = [label_to_code[l] for l in sel_labels if l in label_to_code]
    if not sel_codes:
        st.info("请在左侧至少选择 1 个板块。")
        return

    view = st.radio("视图", SECTOR_VIEWS, horizontal=True, key="sec_view",
                    help="两个视图用同一份板块选择：相关性看谁与谁同涨同跌，轮动看相对强弱的排名迁移。")
    if view == "相关性":
        _render_sector_correlation(sel_codes, name_by_code)
    else:
        _render_sector_rotation(sel_codes, name_by_code)

