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


def volume_ratio(volume: pd.Series, period: int = 5) -> pd.Series:
    """量比：当日成交量 / 过去N日平均成交量"""
    avg_vol = volume.rolling(window=period).mean().shift(1)
    return volume / avg_vol


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               atr_period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """SuperTrend 指标：ATR 自适应趋势跟踪通道

    返回 DataFrame 含 st_upper, st_lower, st_direction, st_value 列
    st_direction: 1=多头, -1=空头
    st_value: 当前生效的轨道价格（多头时为下轨，空头时为上轨）
    """
    # 真实波幅 TR
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder 平滑 ATR
    atr = tr.ewm(alpha=1 / atr_period, min_periods=atr_period, adjust=False).mean()

    # 基础频带
    mid = (high + low) / 2
    basic_upper = mid + multiplier * atr
    basic_lower = mid - multiplier * atr

    # 逐柱计算（频带仅在趋势方向上收紧）
    n = len(close)
    st_upper = np.full(n, np.nan)
    st_lower = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    # 找到第一根 ATR 有效的位置
    first_valid = atr.first_valid_index()
    if first_valid is None:
        return pd.DataFrame({"st_upper": st_upper, "st_lower": st_lower,
                              "st_direction": direction, "st_value": st_upper}, index=close.index)
    start = close.index.get_loc(first_valid)

    st_upper[start] = basic_upper.iloc[start]
    st_lower[start] = basic_lower.iloc[start]
    direction[start] = 1

    for i in range(start + 1, n):
        # 上轨：只能下移（收紧），不能上移
        if basic_upper.iloc[i] < st_upper[i - 1] or close.iloc[i - 1] > st_upper[i - 1]:
            st_upper[i] = basic_upper.iloc[i]
        else:
            st_upper[i] = st_upper[i - 1]

        # 下轨：只能上移（收紧），不能下移
        if basic_lower.iloc[i] > st_lower[i - 1] or close.iloc[i - 1] < st_lower[i - 1]:
            st_lower[i] = basic_lower.iloc[i]
        else:
            st_lower[i] = st_lower[i - 1]

        # 方向翻转
        if direction[i - 1] == 1:
            # 当前多头，价格跌破下轨则翻空
            if close.iloc[i] < st_lower[i]:
                direction[i] = -1
            else:
                direction[i] = 1
        else:
            # 当前空头，价格突破上轨则翻多
            if close.iloc[i] > st_upper[i]:
                direction[i] = 1
            else:
                direction[i] = -1

    direction_s = pd.Series(direction, index=close.index)
    st_upper_s = pd.Series(st_upper, index=close.index)
    st_lower_s = pd.Series(st_lower, index=close.index)
    # st_value：多头看下轨（支撑），空头看上轨（压力）
    st_value = np.where(direction == 1, st_lower, st_upper)

    return pd.DataFrame({
        "st_upper": st_upper_s,
        "st_lower": st_lower_s,
        "st_direction": direction_s,
        "st_value": pd.Series(st_value, index=close.index),
    })


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

    # 量比
    result["vol_ratio"] = volume_ratio(result["volume"])

    # SuperTrend
    st_cfg = TECHNICAL.get("supertrend", {"atr_period": 10, "multiplier": 3.0})
    result = pd.concat([result, supertrend(result["high"], result["low"], result["close"],
                                           st_cfg["atr_period"], st_cfg["multiplier"])], axis=1)

    return result


# ========== 分维度信号函数 ==========

def _signal_macd(df: pd.DataFrame) -> pd.Series:
    """MACD 信号：金叉买入、死叉卖出"""
    sig = pd.Series(0.0, index=df.index)
    dif, dea = df["macd_dif"], df["macd_dea"]
    # 金叉：DIF 上穿 DEA
    golden = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    # 死叉：DIF 下穿 DEA
    death = (dif < dea) & (dif.shift(1) >= dea.shift(1))
    sig.loc[golden] = 1.0
    sig.loc[death] = -1.0
    return sig


def _signal_rsi(df: pd.DataFrame) -> pd.Series:
    """RSI 信号：超卖区回升买入，超买区回落卖出"""
    sig = pd.Series(0.0, index=df.index)
    r = df["rsi"]
    # 从超卖区回升（上穿30）
    buy = (r > 30) & (r.shift(1) <= 30)
    # 从超买区回落（下穿70）
    sell = (r < 70) & (r.shift(1) >= 70)
    sig.loc[buy] = 1.0
    sig.loc[sell] = -1.0
    return sig


def _signal_kdj(df: pd.DataFrame) -> pd.Series:
    """KDJ 信号：K上穿D金叉买入，K下穿D死叉卖出，J值极端区域加强"""
    sig = pd.Series(0.0, index=df.index)
    k, d, j = df["kdj_k"], df["kdj_d"], df["kdj_j"]
    # K 上穿 D
    golden = (k > d) & (k.shift(1) <= d.shift(1))
    # K 下穿 D
    death = (k < d) & (k.shift(1) >= d.shift(1))
    sig.loc[golden] = 1.0
    sig.loc[death] = -1.0
    # J < 0 超卖加强买入，J > 100 超买加强卖出
    sig.loc[golden & (j < 20)] = 1.5
    sig.loc[death & (j > 80)] = -1.5
    return sig


def _signal_ma_cross(df: pd.DataFrame) -> pd.Series:
    """均线交叉信号：短期均线穿越长期均线"""
    sig = pd.Series(0.0, index=df.index)
    if "ma_5" not in df.columns or "ma_20" not in df.columns:
        return sig
    ma5, ma20 = df["ma_5"], df["ma_20"]
    golden = (ma5 > ma20) & (ma5.shift(1) <= ma20.shift(1))
    death = (ma5 < ma20) & (ma5.shift(1) >= ma20.shift(1))
    sig.loc[golden] = 1.0
    sig.loc[death] = -1.0
    return sig


def _signal_ma_trend(df: pd.DataFrame) -> pd.Series:
    """均线多头/空头排列信号"""
    sig = pd.Series(0.0, index=df.index)
    cols = [f"ma_{p}" for p in TECHNICAL["ma_periods"] if f"ma_{p}" in df.columns]
    if len(cols) < 3:
        return sig
    # 多头排列：短期 > 中期 > 长期
    bull = True
    bear = True
    for i in range(len(cols) - 1):
        bull = bull & (df[cols[i]] > df[cols[i + 1]])
        bear = bear & (df[cols[i]] < df[cols[i + 1]])
    sig.loc[bull] = 0.5
    sig.loc[bear] = -0.5
    return sig


def _signal_boll(df: pd.DataFrame) -> pd.Series:
    """布林带信号：突破上轨卖出，跌破下轨买入，回归中轨确认"""
    sig = pd.Series(0.0, index=df.index)
    close = df["close"]
    upper, mid, lower = df["boll_upper"], df["boll_mid"], df["boll_lower"]
    # 价格从下方突破下轨后回升（下轨反弹买入）
    buy = (close > lower) & (close.shift(1) <= lower.shift(1))
    # 价格从上方跌破上轨（上轨回落卖出）
    sell = (close < upper) & (close.shift(1) >= upper.shift(1))
    sig.loc[buy] = 1.0
    sig.loc[sell] = -1.0
    return sig


def _signal_supertrend(df: pd.DataFrame) -> pd.Series:
    """SuperTrend 信号：方向翻转时触发"""
    sig = pd.Series(0.0, index=df.index)
    if "st_direction" not in df.columns:
        return sig
    d = df["st_direction"]
    # 从空翻多（-1 → 1）
    buy = (d == 1) & (d.shift(1) == -1)
    # 从多翻空（1 → -1）
    sell = (d == -1) & (d.shift(1) == 1)
    sig.loc[buy] = 1.0
    sig.loc[sell] = -1.0
    return sig


def _signal_volume_confirm(df: pd.DataFrame) -> pd.Series:
    """成交量确认信号：放量增强信号可信度，缩量降低可信度"""
    if "vol_ratio" not in df.columns:
        return pd.Series(1.0, index=df.index)
    vr = df["vol_ratio"]
    confirm = pd.Series(1.0, index=df.index)
    confirm.loc[vr > 1.5] = 1.5   # 放量：信号增强50%
    confirm.loc[vr > 2.0] = 1.8   # 大幅放量：增强80%
    confirm.loc[vr < 0.5] = 0.5   # 缩量：信号打折50%
    return confirm


# ========== 信号汇总 ==========

# 各维度权重
SIGNAL_COMPONENT_WEIGHTS = {
    "macd": 0.20,
    "rsi": 0.08,
    "kdj": 0.12,
    "ma_cross": 0.12,
    "ma_trend": 0.10,
    "boll": 0.15,
    "supertrend": 0.23,
}


def generate_technical_signals(df: pd.DataFrame) -> pd.Series:
    """基于多维度技术指标生成交易信号

    策略逻辑：
    1. 7个维度独立打分（MACD/RSI/KDJ/均线交叉/均线排列/布林带/SuperTrend）
    2. 加权求和得到原始分数
    3. 成交量确认：放量增强信号，缩量削弱信号
    4. clip 到 -1 ~ 1（绝对刻度，与窗口长度无关）

    Returns:
        Series: -1（强烈卖出）~ +1（强烈买入）
    """
    components = {
        "macd": _signal_macd(df),
        "rsi": _signal_rsi(df),
        "kdj": _signal_kdj(df),
        "ma_cross": _signal_ma_cross(df),
        "ma_trend": _signal_ma_trend(df),
        "boll": _signal_boll(df),
        "supertrend": _signal_supertrend(df),
    }

    # 加权求和
    raw = pd.Series(0.0, index=df.index)
    for name, sig in components.items():
        raw += sig * SIGNAL_COMPONENT_WEIGHTS[name]

    # 成交量确认
    vol_confirm = _signal_volume_confirm(df)
    raw = raw * vol_confirm

    return raw.clip(-1, 1)
