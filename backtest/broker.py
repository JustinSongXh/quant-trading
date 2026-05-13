"""模拟券商：处理 A 股交易规则"""

from dataclasses import dataclass, field
from config.settings import BACKTEST, LIMIT_RULES


@dataclass
class Position:
    symbol: str
    shares: int = 0
    avg_cost: float = 0.0
    buy_date: str = ""  # 买入日期，用于 T+1 判断


@dataclass
class Broker:
    """模拟券商，处理 T+1、涨跌停、手续费"""

    cash: float = BACKTEST["initial_capital"]
    positions: dict[str, Position] = field(default_factory=dict)
    trade_log: list[dict] = field(default_factory=list)

    def can_sell(self, symbol: str, current_date: str) -> bool:
        """T+1 检查：今天买的不能今天卖"""
        pos = self.positions.get(symbol)
        if not pos or pos.shares <= 0:
            return False
        return pos.buy_date < current_date

    def is_limit_up(self, prev_close: float, current_price: float, board: str) -> bool:
        """是否涨停"""
        limit = LIMIT_RULES.get(board, 0.10)
        return current_price >= prev_close * (1 + limit) * 0.999

    def is_limit_down(self, prev_close: float, current_price: float, board: str) -> bool:
        """是否跌停"""
        limit = LIMIT_RULES.get(board, 0.10)
        return current_price <= prev_close * (1 - limit) * 1.001

    def buy(self, symbol: str, price: float, shares: int, date: str):
        """买入"""
        # A 股最小单位 100 股
        shares = (shares // 100) * 100
        if shares <= 0:
            return

        cost = price * shares
        commission = max(cost * BACKTEST["commission_rate"], 5)  # 最低 5 元
        slippage_cost = cost * BACKTEST["slippage"]
        total_cost = cost + commission + slippage_cost

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

    def sell(self, symbol: str, price: float, shares: int, date: str):
        """卖出"""
        pos = self.positions.get(symbol)
        if not pos or pos.shares <= 0:
            return

        shares = min(shares, pos.shares)
        revenue = price * shares
        commission = max(revenue * BACKTEST["commission_rate"], 5)
        stamp_tax = revenue * BACKTEST["stamp_tax_rate"]  # 印花税卖出单边
        slippage_cost = revenue * BACKTEST["slippage"]
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
