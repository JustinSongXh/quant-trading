"""自定义策略页 + AI 生成策略面板（issue #42 从 app.py 拆出；#30）"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from strategy.signals import build_signals
from backtest.engine import run_backtest
from backtest.metrics import calc_metrics

from config.pool import load_stock_pool, get_stock_name
from config.settings import DEFAULT_SIGNAL_SOURCE, SIGNAL_LOOKBACK
from strategy.decision import decide
from strategy.trade_strategy import list_trade_strategies, get_trade_strategy

from ui.shared import SIGNAL_SOURCE_LABELS, get_stock_data, render_strategy_params


# ========== AI 生成策略面板（#30）==========
# 全部状态收在 st.session_state["cg_state"] 这一个 GenState 里，
# 一次用户交互 = 一次 orchestrator.advance()，闸门回灌在 advance 内部跑完。

CG_STATE = "cg_state"
CG_RENAME = "cg_rename_open"


def _cg_reset(provider_key: str, params: dict):
    from strategy.agent_gen import orchestrator

    st.session_state[CG_STATE] = orchestrator.new_state(provider_key, **params)
    st.session_state.pop(CG_RENAME, None)


def _cg_pick_provider():
    """返回 (provider_meta, params)；无可用 provider 时返回 (None, None) 并已给出原因"""
    from strategy.agent_gen import registry

    metas = registry.list_providers()
    if not metas:
        st.warning("没有发现任何代码生成 provider（strategy/agent_gen/providers/ 为空）")
        return None, None

    usable = [m for m in metas if m["available"]]
    if not usable:
        st.warning("没有可用的代码生成 provider：")
        for m in metas:
            st.markdown(f"- **{m['name']}**：{m['reason']}")
        return None, None

    if len(usable) == 1:
        meta = usable[0]
        st.caption(f"**{meta['name']}** — {meta['description']}")
    else:
        labels = {m["name"]: m for m in usable}
        # 默认项走 settings.DEFAULT_CODEGEN_PROVIDER（不可用时自动退到第一个可用的），
        # 而不是 radio 的字母序第一项——否则加一个 provider 就可能悄悄改掉默认行为
        default_key = registry.default_provider_key()
        names = list(labels)
        default_idx = next((i for i, n in enumerate(names)
                            if labels[n]["key"] == default_key), 0)
        chosen = st.radio("生成方式", names, index=default_idx,
                          key="cg_provider", horizontal=True)
        meta = labels[chosen]
        st.caption(meta["description"])
    # 不可用的也提一句，否则用户不知道另一个为什么没出现
    for m in metas:
        if not m["available"]:
            st.caption(f"（{m['name']} 不可用：{m['reason']}）")

    params = render_strategy_params(meta["params"], f"cg_param_{meta['key']}")
    return meta, params


def _cg_owner() -> str:
    """当前浏览器会话的 id：任务的归属。刷新页面就会换一个，旧任务因此被回收。"""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        ctx = get_script_run_ctx()
        if ctx is not None and ctx.session_id:
            return ctx.session_id
    except Exception:
        pass
    return "unknown-session"


def _cg_start_job(provider_meta, params, text: str, synthetic: bool):
    """把这一轮交给独立进程去跑（可杀），不在页面线程里阻塞"""
    from strategy.agent_gen import jobs

    state = st.session_state[CG_STATE]
    # provider 选择/参数以当前表单为准：用户可能在两轮之间改了 effort
    state.provider_key = provider_meta["key"]
    state.provider_params = params
    try:
        jobs.start(_cg_owner(), state.to_dict(), text,
                   smoke_options={"synthetic": True} if synthetic else {},
                   label=text[:40])
    except jobs.JobError as e:
        st.error(str(e))
        return
    st.rerun()


@st.fragment(run_every=1.0)
def _cg_job_fragment() -> None:
    """任务进度片段：每秒自己重跑一次，脚本主体不阻塞。

    用 fragment 而不是「sleep + 整页 rerun」：整页忙轮询会把侧边栏、回测图表
    全部重画一遍，而这里只需要刷一行进度。

    poll() 顺带刷新心跳——一旦本会话不再轮询（刷新/关页面），后台回收线程就会
    在 STALE_SECONDS 内杀掉这个任务。
    """
    from strategy.agent_gen import jobs

    owner = _cg_owner()
    job = jobs.get(owner)
    if job is None:
        return

    out = jobs.poll(owner)
    if out is not None:
        _cg_consume(out)          # 内部 st.rerun()（默认整页），退出片段
        return

    st.info(f"🤔 模型正在思考…（已用 {job.elapsed:.0f} 秒；首轮通常 30~150 秒，"
            "它会先读仓库里的基类和 broker）")
    st.caption("刷新或关闭页面会自动终止这次生成，不会继续消耗额度。")
    if st.button("⏹ 停止", key="cg_cancel"):
        jobs.cancel(owner)
        st.rerun()


def _cg_consume(out: dict) -> None:
    """任务出结果：写回状态。取消不算失败，静默回到原状态。"""
    from strategy.agent_gen import jobs, orchestrator

    jobs.finish(_cg_owner())
    if out.get("ok"):
        st.session_state[CG_STATE] = orchestrator.GenState.from_dict(out["state"])
    elif out.get("cancelled"):
        pass  # 状态没动过，回到发起前的样子
    else:
        state = st.session_state[CG_STATE]
        state.status = orchestrator.ERROR
        state.error = out.get("error", "未知错误")
        if out.get("detail"):
            state.fixes = [orchestrator.FixAttempt(
                gate="runner", reason=state.error, detail=out["detail"])]
    st.rerun()


def _cg_render_fixes(state):
    """闸门回灌只在折叠状态条里出现，堆栈展开才看——不进对话区（#30 5.3）"""
    from strategy.agent_gen import orchestrator

    for i, fix in enumerate(state.fixes, 1):
        label = f"🔧 已自动修正（{i}/{orchestrator.MAX_FIX}）：{fix.reason}"
        with st.expander(label, expanded=False):
            st.code(fix.detail or "（无更多信息）", language="text")


def _cg_render_panel(state):
    """三道闸门逐项反馈 + 冒烟摘要 + 生成代码"""
    st.success("三道闸门全部通过")
    cols = st.columns(3)
    cols[0].metric("闸门1 零写权限", "✅")
    cols[1].metric("闸门2 静态校验", "✅")
    cols[2].metric("闸门3 冒烟回测", "✅" if state.smoke_result else "—")

    if state.smoke_result and state.smoke_result.ok:
        st.markdown(f"**冒烟回测**：{state.smoke_result.summary()}")
    with st.expander(f"📄 生成的代码：{state.key}.py", expanded=False):
        st.code(state.code, language="python")


def _cg_render_saved_list():
    """已生成策略管理：列表 / 查看源码 / 删除"""
    from strategy.agent_gen import store

    items = store.list_custom()
    if not items:
        return
    st.markdown("---")
    st.markdown("**已生成的策略**")
    for item in items:
        label = item["name"] if item["registered"] else f"⚠️ {item['key']}（未能加载）"
        cols = st.columns([6, 1])
        cols[0].markdown(f"`{item['key']}` — {label}")
        if cols[1].button("删除", key=f"cg_del_{item['key']}"):
            store.delete(item["key"])
            st.rerun()
        with st.expander(f"查看 {item['key']}.py", expanded=False):
            try:
                st.code(store.read_source(item["key"]), language="python")
            except store.StoreError as e:
                st.error(str(e))


def _render_codegen_expander():
    from strategy.agent_gen import jobs, orchestrator, store

    with st.expander("🤖 AI 生成新策略", expanded=False):
        provider_meta, params = _cg_pick_provider()
        if provider_meta is None:
            return

        if CG_STATE not in st.session_state:
            _cg_reset(provider_meta["key"], params)
        state = st.session_state[CG_STATE]

        # ---- 有在跑的任务：只渲染历史 + 进度片段，其余 UI 一律不显示 ----
        if jobs.get(_cg_owner()) is not None:
            for msg in state.messages:
                with st.chat_message(msg.role):
                    st.markdown(msg.text)
            _cg_job_fragment()
            return

        synthetic = st.checkbox(
            "冒烟回测用合成行情", value=False, key="cg_synthetic",
            help="默认用真实行情（沪深300 + 一只主板股，近1年）。数据源连不上时勾这个，"
                 "改用固定合成 K 线——仍跑完整回测链路，只是行情是造的。",
        )

        # ---- 对话区 ----
        for msg in state.messages:
            with st.chat_message(msg.role):
                st.markdown(msg.text)
        _cg_render_fixes(state)

        # ---- 各状态专属 UI ----
        if state.status == orchestrator.ASKING:
            st.caption(f"已澄清 {state.asks}/{orchestrator.MAX_ASKS} 轮 —— "
                       "回答上面的问题，或直接让它自己定。")
            if st.button("🙌 你决定就好", key="cg_decide",
                         help="按推荐默认值定稿，不再反问；它替你做的决定会写进策略说明"):
                _cg_start_job(provider_meta, params, orchestrator.DECIDE_FOR_ME, synthetic)

        elif state.status == orchestrator.STUCK:
            st.warning(f"已澄清 {orchestrator.MAX_ASKS} 轮仍未交付。"
                       "可以让它按推荐默认值直接定稿，或重新开始。")
            cols = st.columns(2)
            if cols[0].button("🙌 你决定就好", key="cg_decide_stuck"):
                _cg_start_job(provider_meta, params, orchestrator.DECIDE_FOR_ME, synthetic)
            if cols[1].button("↺ 重新开始", key="cg_restart_stuck"):
                _cg_reset(provider_meta["key"], params)
                st.rerun()

        elif state.status == orchestrator.READY:
            _cg_render_panel(state)
            cols = st.columns([2, 2, 3])
            if cols[0].button("💾 保存并启用", key="cg_save", type="primary"):
                try:
                    orchestrator.promote(state)
                    st.success(f"已启用 {state.key}，在下方策略列表里选它即可回测")
                    _cg_reset(provider_meta["key"], params)
                    st.rerun()
                except store.KeyTakenError:
                    # 闸门2 到这次点击之间被别的标签页抢了名字，给改名入口而不是打回模型
                    st.session_state[CG_RENAME] = True
                    st.rerun()
                except store.StoreError as e:
                    st.error(str(e))
            if cols[1].button("✏️ 换个名字", key="cg_rename_btn"):
                st.session_state[CG_RENAME] = True
            if cols[2].button("↺ 重新开始", key="cg_restart_ready"):
                _cg_reset(provider_meta["key"], params)
                st.rerun()

            if st.session_state.get(CG_RENAME):
                new_key = st.text_input(
                    "新的策略 key（小写字母开头，仅小写字母/数字/下划线）",
                    value=state.key or "", key="cg_new_key",
                )
                if st.button("确认改名并保存", key="cg_rename_ok"):
                    try:
                        orchestrator.rename(state, new_key.strip())
                        orchestrator.promote(state)
                        st.success(f"已启用 {state.key}")
                        _cg_reset(provider_meta["key"], params)
                        st.rerun()
                    except store.StoreError as e:
                        st.error(str(e))

        elif state.status == orchestrator.FAILED:
            st.error(f"自动修正 {orchestrator.MAX_FIX} 次仍未通过闸门，已归档。"
                     f"最后一次的原因：{state.error}")
            st.caption("换个说法再试，或把要求说得更具体一些（尤其是仓位粒度和触发基准）。")
            if st.button("↺ 重新开始", key="cg_restart_failed"):
                _cg_reset(provider_meta["key"], params)
                st.rerun()

        elif state.status == orchestrator.ERROR:
            st.error(f"调用模型失败：{state.error}")
            if st.button("↺ 重新开始", key="cg_restart_error"):
                _cg_reset(provider_meta["key"], params)
                st.rerun()

        # ---- 输入框（终态之外都可以继续说话）----
        if state.can_submit:
            placeholder = ("描述你想要的策略，例如「跌 5% 加仓 20%，涨 10% 清仓一半，最多 5 档」"
                           if state.status == orchestrator.NEW else "回答上面的问题…")
            text = st.chat_input(placeholder, key="cg_input")
            if text:
                _cg_start_job(provider_meta, params, text, synthetic)

        _cg_render_saved_list()


def page_custom_strategy():
    st.title("A股港股量化分析小程序")
    st.subheader("自定义交易策略")
    st.caption("交易策略负责「持仓周期里如何分批进出仓」，与左侧信号源（决定建仓触发时机）组合使用。")

    # 放在 strategies 空值检查之前：把自定义策略全删光时，这个面板仍要能用
    _render_codegen_expander()

    strategies = list_trade_strategies(custom_only=True)
    if not strategies:
        st.info("暂无可用的自定义交易策略。可以用上面的「AI 生成新策略」造一个。")
        return

    # ---- 侧边栏：选股 + 信号源（建仓触发）----
    stocks = load_stock_pool()
    idx_stocks = [s for s in stocks if s.get("type") == "index"]
    sector_stocks = [s for s in stocks if s.get("type") == "sector"]
    a_stocks = [s for s in stocks if s.get("market", "A") == "A" and s.get("type") not in ("index", "sector")]
    hk_stocks = [s for s in stocks if s.get("market") == "HK"]

    market_tabs = ["A股", "港股"]
    if sector_stocks:
        market_tabs = ["板块"] + market_tabs
    if idx_stocks:
        market_tabs = ["指数"] + market_tabs
    market_tab = st.sidebar.radio("市场", market_tabs, horizontal=True, key="cs_market")
    if market_tab == "指数":
        pool = idx_stocks
    elif market_tab == "板块":
        pool = sector_stocks
    elif market_tab == "港股":
        pool = hk_stocks
    else:
        pool = a_stocks

    stock_options = {f"{s['name']}({s['code']})": s for s in pool}
    if not stock_options:
        st.sidebar.warning(f"{market_tab}股票池为空")
        return
    selected = st.sidebar.selectbox("选择股票", list(stock_options.keys()), key="cs_stock")
    sel = stock_options[selected]
    symbol = sel["code"]
    stype = sel.get("type", "stock")
    name = get_stock_name(symbol, stock_type=stype)

    time_range = st.sidebar.selectbox("回测时间范围", ["近3个月", "近6个月", "近1年", "近2年"],
                                       index=2, key="cs_time_range")
    time_range_days = {"近3个月": 90, "近6个月": 180, "近1年": 365, "近2年": 730}[time_range]

    st.sidebar.markdown("---")
    st.sidebar.markdown("**信号源（建仓触发）**")
    sources = list(SIGNAL_SOURCE_LABELS.keys())
    source = st.sidebar.radio(
        "信号源", options=sources, format_func=lambda k: SIGNAL_SOURCE_LABELS[k],
        index=sources.index(DEFAULT_SIGNAL_SOURCE), key="cs_source",
        label_visibility="collapsed",
    )
    st.sidebar.caption("建仓时机由该信号源的买入信号触发；建仓后由所选交易策略接管。")

    # ---- 主面板：选交易策略 + 参数（数据驱动）----
    strat_labels = {s["name"]: s for s in strategies}
    chosen_name = st.radio("交易策略", list(strat_labels.keys()), key="cs_strategy", horizontal=True)
    meta = strat_labels[chosen_name]

    # 参数输入（仅当该策略声明了 PARAMS 才显示，放主面板而非侧边栏）
    param_kwargs = {}
    if meta["params"]:
        st.markdown("**策略参数**")
        param_kwargs = render_strategy_params(meta["params"], f"cs_param_{meta['key']}")

    # 用当前参数实例化策略，读取（含动态阈值文案的）说明与算例
    # 后端 __init__ 会再次校验阈值有效性，这里捕获并提示
    try:
        strategy = get_trade_strategy(meta["key"], **param_kwargs)
    except ValueError as e:
        st.error(f"策略参数无效：{e}")
        st.stop()
    with st.expander(f"📖 策略说明：{strategy.name}", expanded=True):
        st.markdown(strategy.description)
        if strategy.example:
            st.markdown("**算例**")
            st.markdown(strategy.example)

    run = st.button("刷新行情并回测", type="primary", key="cs_run",
                    help="重新抓取最新行情后回测；只调整策略参数无需点此，会自动即时重算")

    # ---- 行情/信号缓存：只随 股票 / 区间 / 信号源 变化而重抓（贵，含网络）----
    # 与策略参数解耦：改阈值、换策略只触发下面约 0.1s 的回测，不重新抓数据。
    data_key = (symbol, stype, time_range, source)
    data_cache = st.session_state.get("cs_data")
    if run or not data_cache or data_cache.get("key") != data_key:
        kronos_progress = st.empty() if source == "kronos" else None
        def _cs_progress(cur, total, msg):
            if kronos_progress:
                if cur < total:
                    kronos_progress.progress(cur / total, text=msg)
                else:
                    kronos_progress.empty()

        with st.spinner(f"正在加载 {name}({symbol}) 行情数据..."):
            buffer_days = SIGNAL_LOOKBACK[source]
            df = get_stock_data(symbol, days=time_range_days + buffer_days,
                                 force_refresh=run, stock_type=stype)
            if df is None or df.empty:
                st.error(f"无法获取 {name}({symbol}) 的行情数据，请稍后重试。")
                st.session_state.pop("cs_data", None)
                st.stop()

            signal_df_full = build_signals(df, symbol=symbol, sentiment_scores=None,
                                            enabled_signals=[source],
                                            progress_cb=_cs_progress if kronos_progress else None)
            fusion_full = decide(signal_df_full, source=source)
            cutoff = signal_df_full.index.max() - pd.Timedelta(days=time_range_days)
            signal_df_bt = signal_df_full.loc[signal_df_full.index >= cutoff]
            fusion_bt = fusion_full.loc[fusion_full.index >= cutoff]

        st.session_state["cs_data"] = {
            "key": data_key, "signal_df_bt": signal_df_bt, "fusion_bt": fusion_bt,
        }
        data_cache = st.session_state["cs_data"]

    signal_df_bt = data_cache["signal_df_bt"]
    fusion_bt = data_cache["fusion_bt"]

    # ---- 回测：随所选策略 / 参数即时重算（纯计算，约 0.1s）----
    result = run_backtest(symbol, signal_df_bt, fusion_bt, stock_type=stype,
                          trade_strategy=strategy)
    metrics = calc_metrics(result["net_values"], result["initial_capital"])
    trades = result["trade_log"]

    # ---- 绩效指标 ----
    st.markdown("---")
    st.subheader(f"{name}({symbol}) 回测结果（{time_range}）")
    cols = st.columns(6)
    labels = ["总收益", "年化收益", "最大回撤", "夏普比率", "胜率", "交易天数"]
    mkeys = ["total_return", "annual_return", "max_drawdown", "sharpe_ratio", "win_rate", "trading_days"]
    for col, label, mk in zip(cols, labels, mkeys):
        val = metrics.get(mk, 0)
        if mk in ("total_return", "annual_return", "max_drawdown", "win_rate"):
            col.metric(label, f"{val}%")
        else:
            col.metric(label, val)

    # ---- 图表：K线+买卖点 / 净值 ----
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        row_heights=[0.68, 0.32], subplot_titles=["K线 & 买卖点", "净值曲线"])
    fig.add_trace(go.Candlestick(
        x=signal_df_bt.index, open=signal_df_bt["open"], high=signal_df_bt["high"],
        low=signal_df_bt["low"], close=signal_df_bt["close"], name="K线",
        increasing_line_color="#e74c3c", decreasing_line_color="#27ae60"), row=1, col=1)

    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    if buys:
        fig.add_trace(go.Scatter(
            x=[t["date"] for t in buys], y=[t["price"] for t in buys], mode="markers+text",
            marker=dict(symbol="triangle-up", size=14, color="#e74c3c", line=dict(width=1, color="white")),
            text=["B"] * len(buys), textposition="bottom center", textfont=dict(size=9, color="#e74c3c"),
            name=f"买入({len(buys)})", hovertext=[f"买入 {t['shares']}股 @ {t['price']:.2f}" for t in buys],
            hoverinfo="text+x"), row=1, col=1)
    if sells:
        fig.add_trace(go.Scatter(
            x=[t["date"] for t in sells], y=[t["price"] for t in sells], mode="markers+text",
            marker=dict(symbol="triangle-down", size=14, color="#27ae60", line=dict(width=1, color="white")),
            text=["S"] * len(sells), textposition="top center", textfont=dict(size=9, color="#27ae60"),
            name=f"卖出({len(sells)})", hovertext=[f"卖出 {t['shares']}股 @ {t['price']:.2f}" for t in sells],
            hoverinfo="text+x"), row=1, col=1)

    nv = result["net_values"]
    if not nv.empty:
        fig.add_trace(go.Scatter(x=nv.index, y=nv["net_value"], name="净值",
                                 line=dict(color="#3498db", width=2), fill="tozeroy",
                                 fillcolor="rgba(52,152,219,0.1)"), row=2, col=1)
        fig.add_hline(y=result["initial_capital"], line_dash="dash", line_color="gray", row=2, col=1)

    fig.update_layout(height=720, xaxis_rangeslider_visible=False,
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, font=dict(size=10)),
                      margin=dict(l=60, r=20, t=40, b=20))
    for i in range(1, 3):
        fig.update_xaxes(showticklabels=True, tickformat="%y/%m", dtick="M2", row=i, col=1)
    st.plotly_chart(fig, use_container_width=True)

    # ---- 交易明细 ----
    if trades:
        st.subheader("交易明细")
        # 当前收益：到该笔为止的累计盈亏（已实现 + 当前持仓按该行价格的浮盈），含手续费
        # = 成交后「现金 + 持股×该行价格 − 初始资金」，持仓部分即 持股×价 − 持股×均价
        init_cap = result["initial_capital"]
        cash, shares, profits = init_cap, 0, []
        for t in trades:
            if t["action"] == "BUY":
                cash -= t.get("cost", 0)
                shares += t["shares"]
            else:
                cash += t.get("revenue", 0)
                shares -= t["shares"]
            profits.append(round(cash + shares * t["price"] - init_cap, 2))

        trade_df = pd.DataFrame(trades)
        trade_df["action"] = trade_df["action"].map({"BUY": "买入", "SELL": "卖出"})
        trade_df.columns = trade_df.columns.map({"date": "日期", "action": "操作", "price": "价格",
                                                  "shares": "数量", "cost": "总成本", "revenue": "净收入",
                                                  "symbol": "代码"}.get)
        trade_df["当前收益"] = profits
        st.dataframe(trade_df, use_container_width=True, hide_index=True)
        st.caption("当前收益 = 到该笔为止的累计盈亏（已实现 + 当前持仓按该行成交价的浮盈，含手续费）；"
                   "末行若仍有持仓，其按最新收盘价的盈亏见上方「总收益」。")
    else:
        st.info("回测期间无交易发生（信号源未触发建仓，或策略未满足进出仓条件）。")


