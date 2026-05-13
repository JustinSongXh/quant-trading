"""Mock 数据生成，用于在无法连接数据源时测试流程"""

import pandas as pd
import numpy as np


def generate_mock_kline(symbol: str, days: int = 365, base_price: float = 100.0) -> pd.DataFrame:
    """生成模拟日K线数据"""
    np.random.seed(hash(symbol) % 2**31)
    dates = pd.bdate_range(end=pd.Timestamp.now(), periods=days)

    # 随机游走生成价格
    returns = np.random.normal(0.0005, 0.02, days)
    close = base_price * np.cumprod(1 + returns)

    # 根据 close 生成 OHLV
    high = close * (1 + np.abs(np.random.normal(0, 0.01, days)))
    low = close * (1 - np.abs(np.random.normal(0, 0.01, days)))
    open_ = low + (high - low) * np.random.random(days)
    volume = np.random.randint(50000, 500000, days).astype(float)
    turnover = volume * close
    pct_change = np.diff(close, prepend=close[0]) / np.roll(close, 1) * 100
    pct_change[0] = 0
    change = np.diff(close, prepend=close[0])
    change[0] = 0
    amplitude = (high - low) / np.roll(close, 1) * 100
    amplitude[0] = 0
    turnover_rate = np.random.uniform(0.5, 5.0, days)

    df = pd.DataFrame({
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "turnover": turnover,
        "amplitude": amplitude,
        "pct_change": pct_change,
        "change": change,
        "turnover_rate": turnover_rate,
    }, index=dates)
    df.index.name = "date"

    return df


# 预设几只股票的基准价格
MOCK_PRICES = {
    "600519": 1800.0,  # 贵州茅台
    "000858": 150.0,   # 五粮液
    "300750": 200.0,   # 宁德时代
}


def fetch_mock_kline(symbol: str, days: int = 365) -> pd.DataFrame:
    """获取 mock K线数据"""
    base_price = MOCK_PRICES.get(symbol, 100.0)
    return generate_mock_kline(symbol, days, base_price)
