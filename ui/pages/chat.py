"""AI 分析助手页（issue #42 从 app.py 拆出；#36）"""

import streamlit as st

from ui.shared import render_strategy_params


CHAT_MSGS = "chat_msgs"          # [{"role", "text", "trace"}]
CHAT_SESSION = "chat_session"    # provider 私有续接状态，原样存回


def _chat_pick_provider():
    """返回 (provider_meta, params)；无可用 provider 时返回 (None, None) 并已给出原因。

    与 _cg_pick_provider 同一套写法（#30）：不可用的也列出来带原因，默认项走
    registry.default_provider_key() 而不是 radio 的字母序第一项。
    """
    from analysis.chat import registry

    metas = registry.list_providers()
    if not metas:
        st.warning("没有发现任何对话 provider（analysis/chat/providers/ 为空）")
        return None, None

    usable = [m for m in metas if m["available"]]
    if not usable:
        st.warning("没有可用的对话 provider：")
        for m in metas:
            st.markdown(f"- **{m['name']}**：{m['reason']}")
        return None, None

    if len(usable) == 1:
        meta = usable[0]
        st.caption(f"**{meta['name']}** — {meta['description']}")
    else:
        labels = {m["name"]: m for m in usable}
        default_key = registry.default_provider_key()
        names = list(labels)
        default_idx = next((i for i, n in enumerate(names)
                            if labels[n]["key"] == default_key), 0)
        chosen = st.radio("对话模型", names, index=default_idx,
                          key="chat_provider", horizontal=True)
        meta = labels[chosen]
        st.caption(meta["description"])
    for m in metas:
        if not m["available"]:
            st.caption(f"（{m['name']} 不可用：{m['reason']}）")

    params = render_strategy_params(meta["params"], f"chat_param_{meta['key']}")
    return meta, params


def _chat_render_trace(trace):
    """工具调用轨迹：**不是可选的 UI 装饰**（#36 四）

    对付的是「解释与实际决策过程不一致」这个已被实证的失效模式——你能看到它到底查了
    什么、传了什么参数、拿回多少行，而不是只看它说了什么。没有这块，回答里的每个数字
    都无从核对。
    """
    if not trace:
        st.caption("⚠️ 本轮没有调用任何工具——回答里的具体数字请勿采信"
                   "（纪律 1：只依据工具返回的数据）。")
        return
    with st.expander(f"🔧 本轮调用了 {len(trace)} 次工具（点开看参数与返回行数）",
                     expanded=False):
        for i, call in enumerate(trace, start=1):
            args = call.get("input") or {}
            arg_text = "、".join(f"{k}={v}" for k, v in args.items()) or "（无参数）"
            line = f"**{i}. `{call.get('tool')}`** — {arg_text} → {call.get('rows')} 行"
            if call.get("error"):
                line += f"　⚠️ {call['error']}"
            st.markdown(line)


def page_chat():
    """AI 分析助手：模型只做意图路由 / 跨源综合 / 追问，算数一律由确定性工具完成"""
    from analysis.chat import registry, tools
    from analysis.chat.prompt import DISCLAIMER

    st.title("AI 分析助手")
    st.caption("用自然语言问「过去一个月哪个板块资金净流入多」「最近哪些板块在轮动」"
               "「600519 基本面怎么样」。模型只负责把问题路由到确定性工具、再把结果讲清楚——"
               "排序/加权/打分全由 Python 完成，它不算数、不预测价格、不产生交易信号。")

    provider_meta, params = _chat_pick_provider()
    if provider_meta is None:
        return

    with st.expander(f"可用工具（{len(tools.TOOLS)} 个，模型只能经它们看数据）", expanded=False):
        for spec in tools.TOOLS:
            st.markdown(f"- **`{spec.name}`**（{spec.data_class} 类）：{spec.description}")
        st.caption("模型没有 Read / Bash / 联网等内置工具，也不加载本机 settings 与 skills。")

    msgs = st.session_state.setdefault(CHAT_MSGS, [])
    for msg in msgs:
        with st.chat_message(msg["role"]):
            st.markdown(msg["text"])
            if msg["role"] == "assistant":
                _chat_render_trace(msg.get("trace"))
                st.caption(DISCLAIMER)

    cols = st.columns([1, 4])
    if cols[0].button("↺ 清空对话", key="chat_clear", disabled=not msgs):
        st.session_state[CHAT_MSGS] = []
        st.session_state.pop(CHAT_SESSION, None)
        st.rerun()

    text = st.chat_input("问点什么…", key="chat_input")
    if not text:
        return

    msgs.append({"role": "user", "text": text})
    provider = registry.get_provider(provider_meta["key"], **params)
    try:
        with st.spinner("模型正在查数据…（会先调工具，再据此作答）"):
            answer = provider.ask(text, st.session_state.get(CHAT_SESSION))
    except Exception as e:
        # 限流/登录态失效这类已经被 provider 翻成人话了，其余带上类型便于排查
        reason = str(e) or f"{type(e).__name__}"
        msgs.append({"role": "assistant", "text": f"⚠️ 调用失败：{reason}", "trace": []})
        st.rerun()

    st.session_state[CHAT_SESSION] = answer.session
    msgs.append({"role": "assistant",
                 "text": "\n\n".join(answer.texts) or "（模型没有返回文本）",
                 "trace": answer.trace})
    st.rerun()

