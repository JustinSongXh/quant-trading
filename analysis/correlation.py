"""板块间相关性分析（#29）

对一组板块的日收益率序列做两两相关系数计算，供「板块相关性」页三个视图使用：
相关性热力图、单板块联动榜、负相关对冲榜。

只复用现有板块 K 线管线（THS 数据源、DuckDB 缓存，见 app.get_stock_data），
不引入新数据源。收益率 = 收盘价 pct_change，按日期 inner join 对齐后 DataFrame.corr。

相关模式（mode）：
- "raw"   原始相关：直接对日收益率算相关，被大盘 beta 主导，成长赛道普遍偏高
- "index" 剔除大盘：每个板块日收益回归掉沪深300日收益，取残差再算相关（暴露真实轮动/对冲）
- "cross" 剔除板块均值：因子改用参与板块的横截面均值，自洽不用额外取数
"""

import pandas as pd

from config.settings import get_stock_name
from data.cache import save_kline, load_kline, is_cache_fresh
from data.fetcher import fetch_daily_kline

VALID_WINDOWS = (20, 60, 120)
VALID_METHODS = ("pearson", "spearman")
VALID_MODES = ("raw", "index", "cross")
MARKET_INDEX = "000300"  # 沪深300 作剔除大盘的市场因子


def _col_label(code: str, seen: dict) -> str:
    """板块列名用中文名；同名（行业/概念可能重名）时退回带 code 区分。"""
    name = get_stock_name(code, "sector")
    if name in seen and seen[name] != code:
        return f"{name}({code})"
    seen[name] = code
    return name


def _get_sector_kline(code: str, days: int, force_refresh: bool = False) -> pd.DataFrame:
    """取板块日K线，逻辑同 app.get_stock_data 的板块分支（sec_ 缓存键 + 新鲜度兜底），
    但不依赖 app.py（避免导入触发 Streamlit 脚本执行）。"""
    cache_key = f"sec_{code}"
    if not force_refresh and is_cache_fresh(cache_key):
        df = load_kline(cache_key)
        if df is not None and not df.empty and (df.index.max() - df.index.min()).days >= days - 7:
            return df
    try:
        df = fetch_daily_kline(code, days=days, stock_type="sector")
        if df is not None and not df.empty:
            save_kline(cache_key, df)
            return df
    except Exception:
        pass
    df = load_kline(cache_key)
    return df if df is not None else pd.DataFrame()


def compute_returns(sector_codes, window=60, force_refresh=False) -> pd.DataFrame:
    """取各板块近 window 交易日的日收益率，按日期 inner join 对齐。

    Returns: DataFrame(index=date, columns=板块名)，列数可能少于入参（取数失败/停牌的板块被丢弃）。
    """
    # window 是"交易日"数，用 window*1.5 + buffer 的日历天兜住周末/停牌/上市不足
    days = int(window * 1.5) + 40
    series = {}
    seen = {}
    for code in sector_codes:
        df = _get_sector_kline(code, days, force_refresh=force_refresh)
        if df is None or df.empty or "close" not in df.columns:
            continue
        ret = df["close"].pct_change().dropna()
        if len(ret) < window // 2:  # 数据太短（新板块/长期停牌），跳过
            continue
        series[_col_label(code, seen)] = ret.tail(window)

    if len(series) < 2:
        return pd.DataFrame()
    # inner join 对齐交易日：只保留所有板块都有数据的日期
    return pd.concat(series, axis=1, join="inner").dropna(how="any")


def _get_index_returns(window, force_refresh=False) -> pd.Series | None:
    """沪深300 日收益率序列，供剔除大盘用。取不到返回 None（由调用方退回原始相关）。"""
    days = int(window * 1.5) + 40
    cache_key = f"idx_{MARKET_INDEX}"
    df = None
    if not force_refresh and is_cache_fresh(cache_key):
        df = load_kline(cache_key)
    if df is None or df.empty:
        try:
            df = fetch_daily_kline(MARKET_INDEX, days=days, stock_type="index")
            if df is not None and not df.empty:
                save_kline(cache_key, df)
        except Exception:
            df = load_kline(cache_key)
    if df is None or df.empty or "close" not in df.columns:
        return None
    return df["close"].pct_change().dropna()


def _residualize(returns: pd.DataFrame, factor: pd.Series) -> pd.DataFrame:
    """把每个板块日收益对 factor 做单因子回归，返回残差（板块特异波动）。

    残差 = y - beta * factor，beta = cov(y, factor) / var(factor)。factor 方差为 0
    或对齐后样本太少时原样返回。
    """
    f = factor.reindex(returns.index).dropna()
    if len(f) < 3:
        return returns
    r = returns.loc[f.index]
    var = f.var()
    if var == 0:
        return returns
    out = {col: r[col] - (r[col].cov(f) / var) * f for col in r.columns}
    return pd.DataFrame(out, index=f.index)


def _model_returns(sector_codes, window, mode, force_refresh=False) -> pd.DataFrame:
    """按 mode 产出用于算相关的收益率：原始 / 剔除大盘 / 剔除板块均值。"""
    returns = compute_returns(sector_codes, window=window, force_refresh=force_refresh)
    if returns.empty or mode == "raw":
        return returns
    if mode == "cross":
        return _residualize(returns, returns.mean(axis=1))
    if mode == "index":
        idx_ret = _get_index_returns(window, force_refresh=force_refresh)
        if idx_ret is None or idx_ret.empty:
            return returns  # 指数取不到就退回原始相关
        return _residualize(returns, idx_ret)
    return returns


def corr_matrix(sector_codes, window=60, method="pearson", mode="raw") -> pd.DataFrame:
    """N×N 相关系数矩阵（对称、对角线=1，columns/index=板块名）。数据不足返回空。

    mode: raw 原始 / index 剔除沪深300 / cross 剔除板块横截面均值。
    """
    returns = _model_returns(sector_codes, window, mode)
    if returns.empty or returns.shape[1] < 2:
        return pd.DataFrame()
    return returns.corr(method=method)


def linkage_ranking(target_code, sector_codes, window=60, top_n=5, method="pearson", mode="raw") -> dict:
    """target 板块对其余板块的相关系数排序，取最正/最负各 top_n。

    Returns: {"target": 板块名, "positive": [(名, 系数), ...], "negative": [(名, 系数), ...]}
             数据不足或 target 不在矩阵中时 positive/negative 为空列表。
    """
    mat = corr_matrix(sector_codes, window=window, method=method, mode=mode)
    target_name = get_stock_name(target_code, "sector")
    if mat.empty or target_name not in mat.columns:
        return {"target": target_name, "positive": [], "negative": []}

    row = mat[target_name].drop(labels=[target_name], errors="ignore").dropna()
    ranked = row.sort_values(ascending=False)
    positive = [(n, float(v)) for n, v in ranked.head(top_n).items()]
    negative = [(n, float(v)) for n, v in ranked.tail(top_n).items()][::-1]  # 最负在前
    return {"target": target_name, "positive": positive, "negative": negative}


def hedge_pairs(sector_codes, window=60, top_n=15, method="pearson", mode="raw") -> list:
    """全局所有板块对里相关系数最低的 top_n 对（升序）。

    Returns: [(板块A, 板块B, 系数), ...]，系数从最负到较高。数据不足返回空列表。
    """
    mat = corr_matrix(sector_codes, window=window, method=method, mode=mode)
    if mat.empty:
        return []
    cols = list(mat.columns)
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = mat.iloc[i, j]
            if pd.notna(v):
                pairs.append((cols[i], cols[j], float(v)))
    pairs.sort(key=lambda p: p[2])
    return pairs[:top_n]


def cluster_order(mat: pd.DataFrame) -> list:
    """对相关矩阵做层次聚类，返回重排后的板块名顺序（抱团板块相邻）。

    板块数 < 3 或 scipy 不可用时原样返回列顺序。距离用 1 - 相关系数。
    """
    cols = list(mat.columns)
    if len(cols) < 3:
        return cols
    try:
        from scipy.cluster.hierarchy import linkage, leaves_list
        from scipy.spatial.distance import squareform
        dist = 1 - mat.values
        dist = (dist + dist.T) / 2  # 强制对称，消除浮点误差
        for i in range(len(dist)):
            dist[i, i] = 0.0
        condensed = squareform(dist, checks=False)
        order = leaves_list(linkage(condensed, method="average"))
        return [cols[i] for i in order]
    except Exception:
        return cols
