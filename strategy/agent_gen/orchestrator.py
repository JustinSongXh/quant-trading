"""生成 → 闸门 → 回灌 → 落盘 主循环（provider 无关）（#30）

两个计数器，互不占用额度：

| | 澄清轮（Turn.kind=="ask"） | 闸门回灌轮 |
|---|---|---|
| 触发 | 模型主动反问 | 闸门失败，自动回灌 |
| 用户参与 | 等用户输入 | 全自动，用户不介入 |
| 上限 | MAX_ASKS = 3 | MAX_FIX = 2 |
| 前端 | 对话气泡 | 折叠的「正在修正（1/2）」状态条 |

Streamlit 每次交互重跑整个脚本，所以这里不做长驻循环：全部状态收在 GenState 里
（放 st.session_state），一次用户交互 = 一次 advance()。advance() 内部会把闸门
回灌跑完（那部分不需要用户参与），返回时 GenState 已是终态之一。

回灌的 traceback 只喂给模型，不进对话区——把 Python 堆栈糊用户一脸没有意义。
前端只读 FixAttempt.reason 当标题，detail 折叠起来。
"""

import logging
import uuid
from dataclasses import dataclass, field

from strategy.agent_gen import guard, smoke, store
from strategy.agent_gen.base import CodegenProvider, Turn
from strategy.agent_gen.extract import prose

logger = logging.getLogger(__name__)

MAX_ASKS = 3     # 澄清轮上限（用户参与）
MAX_FIX = 2      # 闸门失败后的自动修正次数（用户不介入）

# 状态机
NEW = "new"            # 刚建，还没发过话
ASKING = "asking"      # 模型在反问，等用户回应
READY = "ready"        # 三道闸门全过，等用户点「保存并启用」
FAILED = "failed"      # 修正次数用尽，已归档 _rejected
STUCK = "stuck"        # 澄清轮用尽仍未交付，交回用户决定
SAVED = "saved"        # 已落盘启用
ERROR = "error"        # provider 自身出错（网络/限流/凭证）

# 5.2「你决定就好」逃生口：用户说不清楚时最想要的就是这个
DECIDE_FOR_ME = (
    "按你的推荐默认值把所有未定项定下来，直接给实现，不要再问。"
    "在策略说明（description）里列出你替我做的每个决定。"
)


@dataclass
class Message:
    """对话区的一条气泡。回灌轮不产生 Message。"""
    role: str          # "user" / "assistant"
    text: str


@dataclass
class FixAttempt:
    """一次自动修正的记录，渲染成折叠状态条"""
    gate: str          # "guard" / "smoke"
    reason: str        # 一句话原因，当标题
    detail: str        # 完整错误，展开才看


@dataclass
class GenState:
    """一次生成会话的全部状态（放进 st.session_state）"""
    provider_key: str
    provider_params: dict = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    session: dict = field(default_factory=dict)       # provider 私有续接状态
    messages: list[Message] = field(default_factory=list)
    fixes: list[FixAttempt] = field(default_factory=list)
    asks: int = 0
    status: str = NEW
    key: str | None = None
    code: str | None = None
    smoke_result: smoke.SmokeResult | None = None
    error: str = ""

    @property
    def asks_left(self) -> int:
        return max(0, MAX_ASKS - self.asks)

    @property
    def can_submit(self) -> bool:
        """现在是否该显示输入框"""
        return self.status in (NEW, ASKING, STUCK)

    # ---------- 序列化 ----------
    # advance() 跑在独立进程里（见 runner.py），状态要能过 JSON。
    # 不用 pickle：跨进程传 pickle 等于给子进程的输出开一条反序列化执行的路，
    # 而那个子进程正在处理模型生成的内容。

    def to_dict(self) -> dict:
        return {
            "provider_key": self.provider_key,
            "provider_params": self.provider_params,
            "run_id": self.run_id,
            "session": self.session,
            "messages": [{"role": m.role, "text": m.text} for m in self.messages],
            "fixes": [{"gate": f.gate, "reason": f.reason, "detail": f.detail}
                      for f in self.fixes],
            "asks": self.asks,
            "status": self.status,
            "key": self.key,
            "code": self.code,
            "smoke_result": (None if self.smoke_result is None else {
                "ok": self.smoke_result.ok,
                "error": self.smoke_result.error,
                "detail": self.smoke_result.detail,
                "cases": self.smoke_result.cases,
            }),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GenState":
        sr = d.get("smoke_result")
        return cls(
            provider_key=d["provider_key"],
            provider_params=d.get("provider_params") or {},
            run_id=d["run_id"],
            session=d.get("session") or {},
            messages=[Message(**m) for m in d.get("messages", [])],
            fixes=[FixAttempt(**f) for f in d.get("fixes", [])],
            asks=d.get("asks", 0),
            status=d.get("status", NEW),
            key=d.get("key"),
            code=d.get("code"),
            smoke_result=None if sr is None else smoke.SmokeResult(**sr),
            error=d.get("error", ""),
        )


def new_state(provider_key: str, **provider_params) -> GenState:
    return GenState(provider_key=provider_key, provider_params=provider_params)


def restart(state: GenState) -> GenState:
    """重新开始：换新 run_id、清空 session 与对话，保留 provider 选择"""
    return new_state(state.provider_key, **state.provider_params)


def advance(provider: CodegenProvider, state: GenState, user_text: str,
            *, smoke_options: dict | None = None) -> GenState:
    """推进一步：发一句话给 provider，把后续能自动做完的都做完。

    Args:
        provider: 已构造好的 provider 实例（由调用方从 registry 取，便于测试注入）
        state: 会话状态，原地更新并返回
        user_text: 用户这一轮说的话（或 DECIDE_FOR_ME）
        smoke_options: 透传给 smoke.run 的参数，单测用 {"synthetic": True}
    """
    state.messages.append(Message("user", user_text))
    state.fixes = []
    state.error = ""

    try:
        turn = (provider.start(user_text) if not state.session
                else provider.resume(state.session, user_text))
    except Exception as e:
        logger.warning("provider %s 调用失败：%s", state.provider_key, e)
        state.status = ERROR
        state.error = f"{type(e).__name__}: {e}"
        return state

    return _handle_turn(provider, state, turn, smoke_options or {})


def _handle_turn(provider, state: GenState, turn: Turn, smoke_options: dict) -> GenState:
    _record(state, turn)

    if turn.kind == "ask":
        state.asks += 1
        # 5.4 卡死兜底：不无限循环，交回用户，前端给「重新开始」/「你决定就好」
        state.status = STUCK if state.asks >= MAX_ASKS else ASKING
        return state

    return _run_gates(provider, state, turn, smoke_options)


def _record(state: GenState, turn: Turn) -> None:
    state.session = turn.session
    if turn.kind == "deliver":
        # 交付回合只把说明文字放进对话区，源码交给校验面板渲染
        text = prose(turn.texts)
        if text:
            state.messages.append(Message("assistant", text))
        return
    for text in turn.texts:
        if text and text.strip():
            state.messages.append(Message("assistant", text))


def _run_gates(provider, state: GenState, turn: Turn, smoke_options: dict) -> GenState:
    """闸门 2/3 + 自动回灌。全程不需要用户参与。"""
    key, code = turn.key, turn.code

    for attempt in range(MAX_FIX + 1):
        path = store.stage(state.run_id, key, code)
        state.key, state.code = key, code

        gate, reason, detail = _check(state, path, key, code, smoke_options)
        if gate is None:
            state.status = READY
            logger.info("策略 %s 通过全部闸门（第 %d 次尝试）", key, attempt + 1)
            return state

        state.fixes.append(FixAttempt(gate=gate, reason=reason, detail=detail))

        if attempt == MAX_FIX:
            store.reject(state.run_id, key, reason)
            state.status = FAILED
            state.error = reason
            return state

        # 回灌：把完整错误喂给模型，用户不介入
        try:
            turn = provider.resume(state.session, _feedback(gate, detail, attempt))
        except Exception as e:
            store.reject(state.run_id, key, f"回灌时 provider 出错：{e}")
            state.status = ERROR
            state.error = f"{type(e).__name__}: {e}"
            return state

        state.session = turn.session
        if turn.kind != "deliver":
            # 修着修着开始反问 → 交回用户，别继续自动重试
            store.reject(state.run_id, key, "修正过程中转为反问")
            for text in turn.texts:
                if text and text.strip():
                    state.messages.append(Message("assistant", text))
            state.asks += 1
            state.status = STUCK if state.asks >= MAX_ASKS else ASKING
            return state

        if turn.key != key:
            # 模型改名重交：旧 staging 文件不再有用，直接丢掉不进 _rejected
            store.drop_staging(state.run_id, key)
        key, code = turn.key, turn.code

    return state  # 不可达：循环内每条分支都 return


def _check(state: GenState, path, key: str, code: str, smoke_options: dict):
    """跑闸门 2 与 3，返回 (失败的闸门, 一句话原因, 完整错误)；全过则 (None, "", "")

    冒烟结果记到 state 上：前端校验面板要展示「总收益 / 交易次数 / 最终净值」摘要
    （#30 八.6），失败时也要，所以成败都存。
    """
    state.smoke_result = None
    errors = guard.check(code, key)
    if errors:
        return "guard", f"静态校验未通过（{len(errors)} 项）：{errors[0]}", "\n".join(errors)

    result = smoke.run(path, key, **smoke_options)
    state.smoke_result = result
    if not result.ok:
        return "smoke", f"冒烟回测未通过：{result.error}", result.detail or result.error
    return None, "", ""


def _feedback(gate: str, detail: str, attempt: int) -> str:
    """回灌给模型的文本。给足信息（含完整堆栈），但不进对话区。"""
    label = "静态校验（AST 白名单 / 契约结构）" if gate == "guard" else "冒烟回测（真实回测链路）"
    return (
        f"你交付的策略没通过{label}，这是第 {attempt + 1} 次修正机会"
        f"（最多 {MAX_FIX} 次）。完整错误如下：\n\n{detail}\n\n"
        "请直接给出修正后的完整文件，格式不变（STRATEGY_KEY + ```python 代码块）。"
        "不要解释原因，不要只给 diff。"
    )


def promote(state: GenState):
    """用户点「保存并启用」：落盘 + 刷新注册表，之后策略立即可选"""
    if state.status != READY:
        raise store.StoreError(f"当前状态 {state.status} 不可保存")
    path = store.promote(state.run_id, state.key)
    state.status = SAVED
    return path


def rename(state: GenState, new_key: str, *, smoke_options: dict | None = None) -> GenState:
    """改名后重新静态校验并重新暂存。

    用于 promote 撞名（KeyTakenError）和「模型起的名字太丑」两种情况。改名不改行为，
    因此只重跑闸门 2，不重跑冒烟回测——key 不参与 on_bar 逻辑。
    """
    if not state.code:
        raise store.StoreError("当前没有可改名的策略源码")

    new_code = store.rewrite_key(state.code, new_key)
    errors = guard.check(new_code, new_key)
    if errors:
        raise store.StoreError("改名后静态校验未通过：" + "；".join(errors))

    old_key = state.key
    store.stage(state.run_id, new_key, new_code)
    if old_key and old_key != new_key:
        store.drop_staging(state.run_id, old_key)
    state.key, state.code, state.status = new_key, new_code, READY
    return state
