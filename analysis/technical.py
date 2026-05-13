"""技术指标计算（纯 pandas/numpy 实现）"""

import pandas as pd
import numpy as np
from config.settings import TECHNICAL


# ========== 指标计算函数 ==========

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    histogram = (dif - dea) * 2  # A股习惯乘2
    return pd.DataFrame({"macd_dif": dif, "macd_dea": dea, "macd_hist": histogram}, index=close.index)


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.DataFrame:
    lowest = low.rolling(window=period).min()
    highest = high.rolling(window=period).max()
    rsv = (close - lowest) / (highest - lowest) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": j}, index=close.index)


def bollinger_bands(close: pd.Series, period: int, std_dev: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(window=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return pd.DataFrame({"boll_upper": upper, "boll_mid": mid, "boll_lower": lower}, index=close.index)


# ========== 主函数 ==========

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算全部技术指标，返回带指标列的 DataFrame"""
    result = df.copy()

    # MA 均线
    for period in TECHNICAL["ma_periods"]:
        result[f"ma_{period}"] = sma(result["close"], period)

    # MACD
    cfg = TECHNICAL["macd"]
    result = pd.concat([result, macd(result["close"], cfg["fast"], cfg["slow"], cfg["signal"])], axis=1)

    # RSI
    result["rsi"] = rsi(result["close"], TECHNICAL["rsi_period"])

    # KDJ
    result = pd.concat([result, kdj(result["high"], result["low"], result["close"], TECHNICAL["kdj_period"])], axis=1)

    # 布林带
    result = pd.concat([result, bollinger_bands(result["close"], TECHNICAL["boll_period"])], axis=1)

    return result


def generate_technical_signals(df: pd.DataFrame) -> pd.Series:
    """基于技术指标生成交易信号

    返回 Series: 1=买入, -1=卖出, 0=持有
    """
    signals = pd.Series(0, index=df.index)

    # MACD 金叉/死叉
    if "macd_hist" in df.columns:
        macd_hist = df["macd_hist"]
        signals += (macd_hist > 0).astype(int) - (macd_hist < 0).astype(int)

    # RSI 超买超卖
    if "rsi" in df.columns:
        signals.loc[df["rsi"] < 30] += 1
        signals.loc[df["rsi"] > 70] -= 1

    # MA5 上穿/下穿 MA20
    if "ma_5" in df.columns and "ma_20" in df.columns:
        golden_cross = (df["ma_5"] > df["ma_20"]) & (df["ma_5"].shift(1) <= df["ma_20"].shift(1))
        death_cross = (df["ma_5"] < df["ma_20"]) & (df["ma_5"].shift(1) >= df["ma_20"].shift(1))
        signals.loc[golden_cross] += 1
        signals.loc[death_cross] -= 1

    # 归一化到 -1 ~ 1
    max_abs = signals.abs().max()
    if max_abs > 0:
        signals = signals / max_abs

    return signals
