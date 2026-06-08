"""可插拔的「交易策略 / 仓位管理」层

与「信号源」(technical/chanlun/kronos) 解耦：
- 信号源回答"现在该不该买/卖"，产出 -1~+1 的原始信号、经 fusion 得到 decision/strength。
- 交易策略回答"在整段持仓周期里如何分批进出仓"。

回测引擎 (backtest/engine.py) 每根 K 线构造 BarContext 并调用
`strategy.on_bar(broker, ctx)`，由策略自行决定调用 broker.buy / broker.sell，
T+1 / 涨跌停等成交约束复用 broker 自身规则。
"""

from dataclasses import dataclass
from config.settings import POSITION_SIZING


@dataclass
class BarContext:
    """单根 K 线交给交易策略的上下文（不含 broker 状态，broker 直接传入 on_bar）"""
    symbol: str
    date: str
    price: float            # 当根收盘价（买卖均按收盘价成交）
    prev_close: float       # 前一根收盘价（用于涨跌停判断）
    decision: int           # 信号源决策：1=买入 / -1=卖出 / 0=观望
    strength: float         # 信号强度 0~1
    board: str              # 板块：main_board / gem / star / hk / index
    initial_capital: float  # 回测初始资金（分档/单位仓位的计算基准）


def _calc_position_pct(strength: float) -> float:
    """根据信号强度计算仓位比例（默认强度分档策略使用）

    强信号（>0.7）→ 重仓；中信号（0.3-0.7）→ 标准仓；弱信号（<0.3）→ 轻仓
    """
    for tier in POSITION_SIZING:
        if strength >= tier["min_strength"]:
            return tier["position_pct"]
    return POSITION_SIZING[-1]["position_pct"]


class TradeStrategy:
    """交易策略基类

    子类需提供元数据 name / description / example（前端从这里读取并渲染，
    避免文案写死在 app.py），并实现 on_bar()。
    PARAMS 描述可在前端配置的参数，元素形如：
        {"key": "threshold", "label": "价格阈值", "default": 0.10,
         "min": 0.01, "max": 0.50, "step": 0.01, "is_pct": True}
    """

    key = "base"
    name = "交易策略"
    description = ""
    example = ""
    custom = False          # True 表示在「自定义策略」页展示
    PARAMS: list[dict] = []

    def reset(self):
        """每次回测开始前重置内部状态"""

    def on_bar(self, broker, ctx: BarContext):
        """处理一根 K 线，自行调用 broker.buy / broker.sell"""
        raise NotImplementedError


class DefaultStrengthStrategy(TradeStrategy):
    """默认强度分档策略：封装现有 `_calc_position_pct` 行为，保证无回归。

    买入信号 → 按信号强度买入「当前现金 × 分档比例」；卖出信号 → 清仓。
    """

    key = "default"
    name = "默认强度分档"
    description = ("系统默认行为：出现买入信号时，按信号强度分 10% / 20% / 30% 三档"
                   "买入（占当前可用现金的比例）；出现卖出信号时清空持仓。")
    example = ""
    custom = False

    def on_bar(self, broker, ctx: BarContext):
        if ctx.decision == 1 and not broker.is_limit_up(ctx.prev_close, ctx.price, ctx.board):
            position_pct = _calc_position_pct(ctx.strength)
            max_spend = broker.cash * position_pct
            shares = int(max_spend / ctx.price)
            broker.buy(ctx.symbol, ctx.price, shares, ctx.date, ctx.board)
        elif ctx.decision == -1 and not broker.is_limit_down(ctx.prev_close, ctx.price, ctx.board):
            if broker.can_sell(ctx.symbol, ctx.date, ctx.board):
                pos = broker.positions.get(ctx.symbol)
                if pos:
                    broker.sell(ctx.symbol, ctx.price, pos.shares, ctx.date, ctx.board)


class PlayDeadQuarterStrategy(TradeStrategy):
    """装死四分之一：仓位以 25% 为最小单位，只存在 0/25/50/75/100% 五档。

    维护买入价栈 (LIFO)，每档对应栈中一个买入价：加仓压栈、减仓弹栈。
    - 加仓基准 = 栈顶 last_buy_price；当前价 ≤ last_buy_price×(1−threshold) → 加 25%。
    - 减仓基准 = 栈内均价 avg_cost；当前价 ≥ avg_cost×(1+threshold) → 减 25%。
    本策略的 avg_cost 是 LIFO 口径的减仓触发参考价，与 broker 账户的加权成本无关。
    """

    key = "playdead_quarter"
    name = "装死四分之一"
    custom = True
    PARAMS = [
        {"key": "buy_threshold", "label": "加仓阈值（向下）",
         "default": 0.10, "min": 0.01, "max": 0.50, "step": 0.01, "is_pct": True},
        {"key": "sell_threshold", "label": "减仓阈值（向上）",
         "default": 0.10, "min": 0.01, "max": 0.50, "step": 0.01, "is_pct": True},
    ]

    def __init__(self, buy_threshold: float = 0.10, sell_threshold: float = 0.10):
        # 阈值有效性校验（后端兜底，前端 number_input 也会限定范围）
        for label, v in (("加仓阈值", buy_threshold), ("减仓阈值", sell_threshold)):
            if not isinstance(v, (int, float)) or not (0 < v < 1):
                raise ValueError(f"{label}必须是 0 与 1 之间的小数（当前 {v!r}）")
        self.buy_threshold = float(buy_threshold)
        self.sell_threshold = float(sell_threshold)
        # 买入价栈：每项 [成交价, 该档实际成交股数]
        self.buy_stack: list[list] = []

    # ---- 元数据（含动态阈值文案）----
    @property
    def description(self) -> str:
        bt = round(self.buy_threshold * 100, 2)
        st = round(self.sell_threshold * 100, 2)
        bt_s = f"{bt:g}"
        st_s = f"{st:g}"
        return (
            f"仓位只有 0% / 25% / 50% / 75% / 100% 五档。加仓阈值 {bt_s}%、减仓阈值 {st_s}%，"
            "两者可不同。\n\n"
            "**建仓**：空仓时，信号源给出买入信号才买入 25%。\n\n"
            f"**加仓（向下）**：持仓后忽略信号，只看价格——当前价跌破"
            f"「最近一次买入价 ×(1−{bt_s}%)」就加 25%，并把基准刷新为本次成交价，"
            "连续下跌时加仓点逐级压低，直到满仓。\n\n"
            f"**减仓（向上）**：当前价涨过「持仓均价 ×(1+{st_s}%)」就减 25%。"
            "买入价是栈（后进先出）：减仓弹出栈顶，剩余栈重算均价，"
            "因此减仓点锚定均价、逐级抬升。\n\n"
            "**装死**：满仓后继续下跌不再操作；**清仓**：减到 0% 回到空仓，"
            "触发清零的当根 K 线不立即重新建仓，下一根起重新接收买入信号。\n\n"
            "**优点**：买入条件严格（只在持续下跌、逐级跌破阈值时才分批加仓），"
            "卖出条件宽松（均价之上每涨一个阈值就兑现一档），整体偏防守、单笔风险小，"
            "不会在高位追涨重仓。\n\n"
            "**缺点**：对大涨大跌不敏感——成长性好的个股快速拉升时会被逐档卖飞，"
            "容易错过翻倍级别的行情；单边大幅下跌时加仓会一路被套、过早打满仓位，"
            "止跌能力有限，策略容易失效。\n\n"
            "**适用范围**：更适合宽基指数，或估值成熟、波动相对收敛、长期围绕价值中枢"
            "震荡的标的；不适合高成长、强趋势的个股。"
        )

    @property
    def example(self) -> str:
        bt = self.buy_threshold
        st = self.sell_threshold
        bt_s = f"{round(bt * 100, 2):g}"
        st_s = f"{round(st * 100, 2):g}"
        p1 = 100.0
        p2 = round(p1 * (1 - bt), 1)
        p3 = round(p2 * (1 - bt), 1)
        avg2 = round((p1 + p2) / 2, 2)
        avg3 = round((p1 + p2 + p3) / 3, 2)
        rprice = round(avg3 * (1 + st), 1)
        return (
            f"以当前阈值（加仓 {bt_s}% / 减仓 {st_s}%，按收盘价成交）为例：\n\n"
            f"- 100 收到买入信号 → 建仓 25%，栈 [100]，均价 100\n"
            f"- 跌到 {p2:g}（≤100×(1−{bt_s}%)）→ 加仓，栈 [100, {p2:g}]，均价 {avg2:g}\n"
            f"- 跌到 {p3:g}（≤{p2:g}×(1−{bt_s}%)）→ 加仓，栈 [100, {p2:g}, {p3:g}]，均价 {avg3:g}\n"
            f"- 涨到 {rprice:g}（≥均价 {avg3:g}×(1+{st_s}%)）→ 减仓一档，弹出 {p3:g}，"
            f"栈 [100, {p2:g}]，均价回到 {avg2:g}"
        )

    @property
    def last_buy_price(self):
        return self.buy_stack[-1][0] if self.buy_stack else None

    @property
    def avg_cost(self):
        if not self.buy_stack:
            return None
        return sum(p for p, _ in self.buy_stack) / len(self.buy_stack)

    def reset(self):
        self.buy_stack = []

    def _buy_unit(self, broker, ctx: BarContext) -> bool:
        """买入一档；成功成交返回 True 并压栈。

        每档花掉「当前现金 × 1/(4−已持档数)」=> ¼ / ⅓ / ½ / 全部，
        因此每档恰好部署约 25% 的初始资金，且末档全仓买入真正达到满仓。
        末档留 0.5% 手续费/滑点余量，避免因费用超出现金而买不进。
        """
        step = len(self.buy_stack)               # 0..3
        frac = 1.0 / (4 - step)
        if frac >= 1.0:
            frac = 0.995
        shares = int(broker.cash * frac / ctx.price)
        if shares <= 0:
            return False
        before = broker.positions[ctx.symbol].shares if ctx.symbol in broker.positions else 0
        broker.buy(ctx.symbol, ctx.price, shares, ctx.date, ctx.board)
        after = broker.positions[ctx.symbol].shares if ctx.symbol in broker.positions else 0
        filled = after - before
        if filled <= 0:
            return False
        self.buy_stack.append([ctx.price, filled])
        return True

    def _sell_unit(self, broker, ctx: BarContext) -> bool:
        """减仓一档（弹出栈顶并卖出对应股数）；成功成交返回 True"""
        if not self.buy_stack:
            return False
        pos = broker.positions.get(ctx.symbol)
        if not pos or pos.shares <= 0:
            self.buy_stack = []
            return False
        _, sh = self.buy_stack[-1]
        sell_sh = min(sh, pos.shares)
        broker.sell(ctx.symbol, ctx.price, sell_sh, ctx.date, ctx.board)
        self.buy_stack.pop()
        return True

    def on_bar(self, broker, ctx: BarContext):
        buy_th = self.buy_threshold
        sell_th = self.sell_threshold
        sold_this_bar = False
        # 单根 K 线逐档迭代：每动一档后用更新后的基准价再判断（实际多为 ≤1 档/根）
        while True:
            step = len(self.buy_stack)

            # A. 空仓：仅信号源买入信号触发建仓；清零当根不重建
            if step == 0:
                if (not sold_this_bar and ctx.decision == 1
                        and not broker.is_limit_up(ctx.prev_close, ctx.price, ctx.board)):
                    if self._buy_unit(broker, ctx):
                        continue
                break

            # C. 满仓装死：仅可减仓
            if step >= 4:
                if ctx.price >= self.avg_cost * (1 + sell_th):
                    if (not broker.is_limit_down(ctx.prev_close, ctx.price, ctx.board)
                            and broker.can_sell(ctx.symbol, ctx.date, ctx.board)):
                        if self._sell_unit(broker, ctx):
                            sold_this_bar = True
                            continue
                break

            # B. 持仓中 (25/50/75%)：价格驱动，加/减仓互斥
            if ctx.price <= self.last_buy_price * (1 - buy_th):
                if not broker.is_limit_up(ctx.prev_close, ctx.price, ctx.board):
                    if self._buy_unit(broker, ctx):
                        continue
                break  # 触及涨停无法加仓 → 顺延下一根
            if ctx.price >= self.avg_cost * (1 + sell_th):
                if (not broker.is_limit_down(ctx.prev_close, ctx.price, ctx.board)
                        and broker.can_sell(ctx.symbol, ctx.date, ctx.board)):
                    if self._sell_unit(broker, ctx):
                        sold_this_bar = True
                        continue
                break  # T+1 / 跌停无法减仓 → 顺延下一根
            break


# ========== 注册表 ==========
_REGISTRY = {
    DefaultStrengthStrategy.key: DefaultStrengthStrategy,
    PlayDeadQuarterStrategy.key: PlayDeadQuarterStrategy,
}
DEFAULT_TRADE_STRATEGY = DefaultStrengthStrategy.key


def list_trade_strategies(custom_only: bool = False) -> list[dict]:
    """列出可用交易策略的元数据（前端渲染用）

    Args:
        custom_only: 仅返回自定义策略（custom=True），用于「自定义策略」页
    """
    out = []
    for key, cls in _REGISTRY.items():
        inst = cls()
        if custom_only and not inst.custom:
            continue
        out.append({
            "key": key,
            "name": inst.name,
            "description": inst.description,
            "example": inst.example,
            "params": cls.PARAMS,
        })
    return out


def get_trade_strategy(key: str, **params) -> TradeStrategy:
    """按 key 构造交易策略实例，params 透传给构造函数"""
    if key not in _REGISTRY:
        raise ValueError(f"unknown trade strategy: {key}")
    return _REGISTRY[key](**params)
