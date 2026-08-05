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


def pairs_from_matrix(mat: pd.DataFrame, top_n: int = 15,
                      positive: bool = False) -> list:
    """已算好的相关矩阵 → 极值板块对。`positive=False` 取最负（对冲），True 取最正（抱团）。

    吃矩阵而不是板块列表：页面上矩阵已经算过一次，再让 hedge_pairs 内部重算一遍，
    95 个板块时等于把取数与计算白做一遍。NaN 对直接跳过（对齐后无重叠交易日的板块）。
    """
    if mat is None or mat.empty:
        return []
    cols = list(mat.columns)
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = mat.iloc[i, j]
            if pd.notna(v):
                pairs.append((cols[i], cols[j], float(v)))
    pairs.sort(key=lambda p: p[2], reverse=positive)
    return pairs[:top_n]


def hedge_pairs(sector_codes, window=60, top_n=15, method="pearson", mode="raw") -> list:
    """全局所有板块对里相关系数最低的 top_n 对（升序）。

    Returns: [(板块A, 板块B, 系数), ...]，系数从最负到较高。数据不足返回空列表。
    """
    mat = corr_matrix(sector_codes, window=window, method=method, mode=mode)
    return pairs_from_matrix(mat, top_n=top_n, positive=False)


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


DEFAULT_MAX_GROUPS = 8


def cluster_groups(mat: pd.DataFrame, max_groups: int = DEFAULT_MAX_GROUPS) -> list[dict]:
    """相关矩阵 → 抱团分组，按组内平均相关系数降序（#29 评论：大 N 下的读法）。

    板块多到几十个时热力图已经读不出东西（95×95 = 9025 格），但「谁和谁抱成一团」这个
    结论仍然要能拿到——所以把 `cluster_order` 的层次聚类往前推一步，直接切成组。
    距离与 linkage 方法与 `cluster_order` 完全一致（`1 - 相关系数` + average），
    两个视图讲的是同一套聚类，不然「重排后相邻」与「分在同一组」会对不上。

    Args:
        max_groups: 最多切几组（fcluster 的 maxclust 上限，实际组数可能更少）

    Returns:
        `[{"members": [板块名...], "avg_corr": 组内两两平均相关 | None}]`，按 avg_corr 降序，
        单成员组的 avg_corr 为 None（一个板块没有「组内相关」，给 0 会被当成不相关）。
        板块数 < 3 或 scipy 不可用 → 全员一组（不抛异常，页面照样能渲染）。
    """
    cols = list(mat.columns) if mat is not None and not mat.empty else []
    if not cols:
        return []

    labels = None
    if len(cols) >= 3:
        try:
            from scipy.cluster.hierarchy import fcluster, linkage
            from scipy.spatial.distance import squareform
            dist = 1 - mat.values
            dist = (dist + dist.T) / 2      # 强制对称，消除浮点误差
            for i in range(len(dist)):
                dist[i, i] = 0.0
            condensed = squareform(dist, checks=False)
            k = max(1, min(int(max_groups), len(cols)))
            labels = fcluster(linkage(condensed, method="average"), t=k,
                              criterion="maxclust")
        except Exception:
            labels = None
    if labels is None:
        labels = [1] * len(cols)

    buckets: dict[int, list[str]] = {}
    for name, label in zip(cols, labels):
        buckets.setdefault(int(label), []).append(name)

    groups = []
    for members in buckets.values():
        groups.append({"members": members, "avg_corr": _avg_pair_corr(mat, members)})
    # 抱团最紧的排前面；avg_corr 为 None（单成员）的排最后
    groups.sort(key=lambda g: (g["avg_corr"] is None, -(g["avg_corr"] or 0.0)))
    return groups


def _avg_pair_corr(mat: pd.DataFrame, members: list[str]) -> float | None:
    """组内两两相关系数的均值（不含对角线）。成员 < 2 或全是 NaN → None"""
    if len(members) < 2:
        return None
    sub = mat.loc[members, members]
    vals = [sub.iloc[i, j] for i in range(len(members)) for j in range(i + 1, len(members))
            if pd.notna(sub.iloc[i, j])]
    if not vals:
        return None
    return round(float(sum(vals) / len(vals)), 4)
