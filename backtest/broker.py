"""模拟券商：处理 A 股 / 港股交易规则"""

from dataclasses import dataclass, field
from config.settings import BACKTEST, BACKTEST_HK, LIMIT_RULES


@dataclass
class Position:
    symbol: str
    shares: int = 0
    avg_cost: float = 0.0
    buy_date: str = ""  # 买入日期，用于 T+1 判断


@dataclass
class Broker:
    """模拟券商，处理 A股T+1 / 港股T+0、涨跌停、手续费"""

    cash: float = BACKTEST["initial_capital"]
    positions: dict[str, Position] = field(default_factory=dict)
    trade_log: list[dict] = field(default_factory=list)

    def _get_config(self, board: str) -> dict:
        """根据板块获取对应的交易参数"""
        if board == "hk":
            return BACKTEST_HK
        if board == "index":
            # 指数：使用 A 股参数但 lot_size=1（按点位模拟交易）
            cfg = dict(BACKTEST)
            cfg["lot_size"] = 1
            return cfg
        return BACKTEST

    def can_sell(self, symbol: str, current_date: str, board: str = "main_board") -> bool:
        """T+N 检查：A股T+1今天买的不能卖，港股T+0可以"""
        pos = self.positions.get(symbol)
        if not pos or pos.shares <= 0:
            return False
        if board == "hk":
            return True  # 港股 T+0
        return pos.buy_date < current_date

    def is_limit_up(self, prev_close: float, current_price: float, board: str) -> bool:
        """是否涨停（港股/指数无涨跌停）"""
        if board in ("hk", "index"):
            return False
        limit = LIMIT_RULES.get(board, 0.10)
        return current_price >= prev_close * (1 + limit) * 0.999

    def is_limit_down(self, prev_close: float, current_price: float, board: str) -> bool:
        """是否跌停（港股/指数无涨跌停）"""
        if board in ("hk", "index"):
            return False
        limit = LIMIT_RULES.get(board, 0.10)
        return current_price <= prev_close * (1 - limit) * 1.001

    def buy(self, symbol: str, price: float, shares: int, date: str, board: str = "main_board"):
        """买入"""
        cfg = self._get_config(board)
        lot_size = cfg.get("lot_size", 100)

        # 按手取整；若不够一手但够至少1股，按实际股数买（回测允许碎股）
        rounded = (shares // lot_size) * lot_size
        shares = rounded if rounded > 0 else shares
        if shares <= 0:
            return

        cost = price * shares
        commission = max(cost * cfg["commission_rate"], cfg.get("min_commission", 5))
        # 港股印花税买卖双边
        stamp_tax = cost * cfg["stamp_tax_rate"] if board == "hk" else 0
        slippage_cost = cost * cfg["slippage"]
        total_cost = cost + commission + stamp_tax + slippage_cost

        if total_cost > self.cash:
            return

        self.cash -= total_cost

        if symbol in self.positions:
            pos = self.positions[symbol]
            total_shares = pos.shares + shares
            pos.avg_cost = (pos.avg_cost * pos.shares + cost) / total_shares
            pos.shares = total_shares
            pos.buy_date = date
        else:
            self.positions[symbol] = Position(symbol=symbol, shares=shares, avg_cost=price, buy_date=date)

        self.trade_log.append({"date": date, "symbol": symbol, "action": "BUY", "price": price, "shares": shares, "cost": total_cost})

    def sell(self, symbol: str, price: float, shares: int, date: str, board: str = "main_board"):
        """卖出"""
        pos = self.positions.get(symbol)
        if not pos or pos.shares <= 0:
            return

        cfg = self._get_config(board)
        shares = min(shares, pos.shares)
        revenue = price * shares
        commission = max(revenue * cfg["commission_rate"], cfg.get("min_commission", 5))
        stamp_tax = revenue * cfg["stamp_tax_rate"]  # A股卖出单边 / 港股双边
        slippage_cost = revenue * cfg["slippage"]
        net_revenue = revenue - commission - stamp_tax - slippage_cost

        self.cash += net_revenue
        pos.shares -= shares

        if pos.shares == 0:
            del self.positions[symbol]

        self.trade_log.append({"date": date, "symbol": symbol, "action": "SELL", "price": price, "shares": shares, "revenue": net_revenue})

    def total_value(self, current_prices: dict[str, float]) -> float:
        """计算总资产"""
        stock_value = sum(pos.shares * current_prices.get(pos.symbol, 0) for pos in self.positions.values())
        return self.cash + stock_value
