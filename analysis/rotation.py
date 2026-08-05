"""板块轮动的确定性计算（issue #34）

「轮动」在这里的定义：**板块相对强弱的排名随时间迁移**——谁在持续走强、谁刚新晋走强、
谁在转弱，配上资金流作为佐证。全是纯计算 + 已有取数管线，零 token。

**覆盖范围是刻意不对称的**：

- 动量类指标（`momentum_table` / `rotation_signals` / `hotness`）只算**股票池里配置的
  板块**。板块日K要逐个取（每板块一次 akshare 调用），90 个行业 + 380 多个概念全算
  不现实。
- 资金流排行则是**全市场**——它一次调用就返回全部板块且已排好序，是免费的（见
  `data/fund_flow.py`）。页面因此同时有「全局视角」和「你关注的板块的深度指标」。

取数一律复用既有管线（`data/kline.py::get_kline` 带 DuckDB 缓存、
`analysis/correlation.py::index_returns` 取沪深300，口径与「板块相关性」页完全一致），
不引入新数据源。

收益 / 超额收益的单位是 **%**（pp）；资金净额的单位是 **亿元**。
"""

import pandas as pd

import config.settings as cfg
from analysis.correlation import MARKET_INDEX, index_returns
from config.pool import get_sector_info, get_stock_name
from data.fund_flow import net_by_sector
from data.kline import get_kline

VALID_WINDOWS = (5, 20, 60)
# 轮动标签用「短窗 vs 长窗」的排名迁移判定；热度分固定用 20 日窗口
SHORT_WINDOW, LONG_WINDOW = 5, 20
HOTNESS_WINDOW = 20
HOTNESS_FLOW_PERIOD = "20日"     # 资金流五档之一，与 HOTNESS_WINDOW 的 20 交易日同量级

LABEL_STRONG = "持续走强"
LABEL_EMERGING = "新晋走强"
LABEL_FADING = "转弱"
LABEL_LAGGARD = "持续弱势"
# 判定顺序即优先级：一个板块只给一个标签，先匹配到的赢（否则「转弱」和「持续弱势」
# 会同时挂在同一个板块上，badge 分组看着自相矛盾）
LABELS = (LABEL_STRONG, LABEL_EMERGING, LABEL_FADING, LABEL_LAGGARD)

# 热度分权重的 key（值在 config/settings.py::SECTOR_HOTNESS_WEIGHTS，运行时读取）
W_EXCESS = "excess_20d"
W_NET = "net_20d"


def col_ret(w: int) -> str:
    return f"{w}日收益"


def col_excess(w: int) -> str:
    return f"{w}日超额"


def col_rank(w: int) -> str:
    return f"{w}日排名"


def _label(code: str, seen: dict) -> str:
    """板块显示名；池外板块退回代码，同名时带 code 区分（同 correlation._col_label）。"""
    info = get_sector_info(code)
    name = info[0] if info else get_stock_name(code, "sector")
    if name in seen and seen[name] != code:
        return f"{name}({code})"
    seen[name] = code
    return name


def _cum_return(returns: pd.Series | None, w: int) -> float | None:
    """近 w 个交易日的累计收益（%）。样本不足 w 天返回 None（新板块/长期停牌）。"""
    if returns is None or len(returns) < w:
        return None
    return round(float((1 + returns.tail(w)).prod() - 1) * 100, 3)


def momentum_table(sector_codes, windows=VALID_WINDOWS,
                   force_refresh: bool = False) -> pd.DataFrame:
    """各板块在各窗口的绝对收益 / 超额收益 / 池内排名。

    超额收益 = 板块累计收益 − 沪深300 同窗口累计收益。**沪深300 取不到时退回绝对收益**
    （同 `correlation._model_returns` 的既有降级行为），并把 `attrs["index_available"]`
    置 False，由页面标注——这时「超额」列其实是绝对收益，不标注会被误读。

    Returns:
        DataFrame(index=板块名, columns=[code, 5日收益, 5日超额, 5日排名, 20日…])。
        数据不足的板块**保留行**、指标为 None（页面要能看出它被跳过了，而不是消失）。
        排名 1 = 该窗口超额收益最高；空表时返回空 DataFrame。
        attrs: index_available / market_index / windows。
    """
    windows = tuple(int(w) for w in windows)
    if not sector_codes or not windows:
        return pd.DataFrame()

    max_w = max(windows)
    days = int(max_w * 1.5) + 40      # 交易日 → 日历天，兜住周末/停牌（同 correlation）
    idx_ret = index_returns(max_w, force_refresh=force_refresh)
    index_ok = idx_ret is not None and not idx_ret.empty
    idx_cum = {w: (_cum_return(idx_ret, w) if index_ok else None) for w in windows}

    rows, seen = [], {}
    for code in sector_codes:
        row = {"板块": _label(code, seen), "code": code}
        df = get_kline(code, days, stock_type="sector", force_refresh=force_refresh)
        ret = None
        if df is not None and not df.empty and "close" in df.columns:
            ret = df["close"].pct_change().dropna()
        for w in windows:
            r = _cum_return(ret, w)
            row[col_ret(w)] = r
            if r is None:
                row[col_excess(w)] = None
            elif idx_cum[w] is None:
                row[col_excess(w)] = r       # 指数缺失：退回绝对收益（见 docstring）
            else:
                row[col_excess(w)] = round(r - idx_cum[w], 3)
        rows.append(row)

    mom = pd.DataFrame(rows).set_index("板块")
    for w in windows:
        # 显式转数值：None 混着 float 会让列变 object dtype，rank / 染色都会炸
        for c in (col_ret(w), col_excess(w)):
            mom[c] = pd.to_numeric(mom[c], errors="coerce")
        mom[col_rank(w)] = mom[col_excess(w)].rank(ascending=False, method="min")
    mom = mom[["code"] + [c for w in windows
                          for c in (col_ret(w), col_excess(w), col_rank(w))]]
    mom.attrs["index_available"] = index_ok
    mom.attrs["market_index"] = MARKET_INDEX
    mom.attrs["windows"] = windows
    return mom


def _third(n: int) -> float:
    """池内「1/3」的名次数。池子太小时兜到 1，否则 n=2 时前 1/3 是 0.67 个板块，
    任何板块都进不去，标签永不触发。"""
    return max(1.0, n / 3)


def rotation_signals(mom: pd.DataFrame, short: int = SHORT_WINDOW,
                     long: int = LONG_WINDOW) -> list[dict]:
    """排名迁移 → 确定性轮动标签。阈值写死在这里（`_third` = 池内 1/3 的名次数）：

    | 标签 | 判定 |
    |---|---|
    | 持续走强 | 短窗、长窗超额均 > 0 且长窗排名在池内前 1/3 |
    | 新晋走强 | 短窗排名比长窗上升 ≥ 池内 1/3 名次，且短窗超额 > 0 |
    | 转弱 | 短窗排名比长窗下降 ≥ 池内 1/3 名次，或短窗超额 < 0 而长窗超额 > 0 |
    | 持续弱势 | 短窗、长窗超额均 < 0 |

    边界一律「**达到即触发**」（`>=` / `<=`）。一个板块只出一个标签，优先级见 `LABELS`。
    指标缺失（数据不足）的板块直接跳过，不猜标签。

    Returns:
        `[{sector, label, rank_short, rank_long, rank_change, excess_short,
           excess_long, detail}]`；`rank_change` > 0 表示名次上升（数字变小）。
        列缺失 / 无有效板块时返回 []。
    """
    need = [col_excess(short), col_excess(long), col_rank(short), col_rank(long)]
    if mom is None or mom.empty or any(c not in mom.columns for c in need):
        return []

    valid = mom.dropna(subset=need)
    n = len(valid)
    if n == 0:
        return []
    third = _third(n)

    out = []
    for name, r in valid.iterrows():
        es, el = float(r[col_excess(short)]), float(r[col_excess(long)])
        rs, rl = float(r[col_rank(short)]), float(r[col_rank(long)])
        change = rl - rs                       # >0 名次上升
        if es > 0 and el > 0 and rl <= third:
            label = LABEL_STRONG
            detail = f"{short}/{long}日超额均为正，{long}日排名第 {rl:.0f}（池内前 1/3）"
        elif change >= third and es > 0:
            label = LABEL_EMERGING
            detail = f"排名由第 {rl:.0f} 升至第 {rs:.0f}（升 {change:.0f} 名），{short}日超额 {es:+.2f}%"
        elif change <= -third or (es < 0 < el):
            label = LABEL_FADING
            detail = (f"排名由第 {rl:.0f} 降至第 {rs:.0f}（降 {-change:.0f} 名）"
                      if change <= -third else
                      f"{long}日超额 {el:+.2f}% 但 {short}日已转负（{es:+.2f}%）")
        elif es < 0 and el < 0:
            label = LABEL_LAGGARD
            detail = f"{short}/{long}日超额均为负（{es:+.2f}% / {el:+.2f}%）"
        else:
            continue
        out.append({
            "sector": name, "label": label,
            "rank_short": rs, "rank_long": rl, "rank_change": change,
            "excess_short": es, "excess_long": el, "detail": detail,
        })
    out.sort(key=lambda d: (LABELS.index(d["label"]), d["rank_short"]))
    return out


def zscore(values: list[float | None]) -> list[float | None]:
    """池内 z-score（样本标准差 ddof=1）。有效值 < 2 个或标准差为 0 时全给 0.0——
    此时该分项无区分度，不该左右排序。None 原样保留（缺失不当 0）。

    #41 起是公共名（原 `_zscore`）：`analysis/sector_pick.py` 有意复用它以保证两处
    z-score 口径一致，被别的模块依赖的东西就该是公共 API。
    """
    got = [v for v in values if v is not None]
    if len(got) < 2:
        return [None if v is None else 0.0 for v in values]
    s = pd.Series(got, dtype="float64")
    mu, sd = float(s.mean()), float(s.std())
    if sd == 0:
        return [None if v is None else 0.0 for v in values]
    return [None if v is None else round((v - mu) / sd, 3) for v in values]


def hotness(sector_codes, force_refresh: bool = False) -> pd.DataFrame:
    """池内板块「热门」的确定性定义：

        热度分 = w_excess · z(20日超额收益) + w_net · z(20日资金净额)

    两项都在**池内**做 z-score，权重取 `config/settings.py::SECTOR_HOTNESS_WEIGHTS`
    （运行时读，改了就生效）。为什么写死成公式而不让模型判断「热门」：同一个问题问两次
    模型对「热门」的理解会飘，而你看不出来；公式至少是可复现、可调的。

    资金净额按**板块名**与资金流排行 join（池内配置名与 THS 资金流接口同源，实测 10/10
    命中）。**join 不上的板块热度分为 None 并在「备注」里说明，不静默当 0**——当 0 会
    把它排到中间，看起来像「不冷不热」，那是假的。join 失败在生产上真会发生：资金流接口
    内部分页，某次调用可能整批漏掉某个板块（见 `data/fund_flow.py::_fetch`）。

    Returns:
        DataFrame(index=板块名, columns=[code, 20日超额, 20日资金净额, z_超额, z_资金,
        热度分, 备注])，按热度分降序，None 排最后。
    """
    mom = momentum_table(sector_codes, windows=(HOTNESS_WINDOW,),
                         force_refresh=force_refresh)
    if mom.empty:
        return pd.DataFrame()

    # 每个 kind 只取一次全市场排行（一次调用返回全部板块，见 data/fund_flow.py）
    nets: dict[str, dict[str, float]] = {}
    excess_col = col_excess(HOTNESS_WINDOW)

    rows = []
    for name, r in mom.iterrows():
        code = r["code"]
        info = get_sector_info(code)
        note = ""
        net = None
        if not info:
            note = "板块不在股票池配置里，无行业/概念归属，资金流未匹配"
        else:
            sector_name, kind = info
            if kind not in nets:
                nets[kind] = net_by_sector(kind, HOTNESS_FLOW_PERIOD)
            if not nets[kind]:
                note = f"{HOTNESS_FLOW_PERIOD}资金流取数失败，资金流未匹配"
            else:
                net = nets[kind].get(sector_name)
                if net is None:
                    note = f"资金流排行里没有「{sector_name}」（{kind}），未匹配"
        excess = r[excess_col]
        excess = None if pd.isna(excess) else float(excess)
        if excess is None and not note:
            note = f"板块日K数据不足 {HOTNESS_WINDOW} 个交易日"
        rows.append({"板块": name, "code": code, excess_col: excess,
                     f"{HOTNESS_WINDOW}日资金净额": net, "备注": note})

    z_ex = zscore([r[excess_col] for r in rows])
    z_net = zscore([r[f"{HOTNESS_WINDOW}日资金净额"] for r in rows])
    weights = getattr(cfg, "SECTOR_HOTNESS_WEIGHTS", {})
    w_ex = float(weights.get(W_EXCESS, 1.0))
    w_net = float(weights.get(W_NET, 1.0))
    for r, ze, zn in zip(rows, z_ex, z_net):
        r["z_超额"], r["z_资金"] = ze, zn
        # 任一分项缺失 → 热度分 None（不拿 0 顶替，见 docstring）
        r["热度分"] = (None if ze is None or zn is None
                    else round(w_ex * ze + w_net * zn, 3))

    out = pd.DataFrame(rows).set_index("板块")
    out = out[["code", excess_col, f"{HOTNESS_WINDOW}日资金净额",
               "z_超额", "z_资金", "热度分", "备注"]]
    for c in (excess_col, f"{HOTNESS_WINDOW}日资金净额", "z_超额", "z_资金", "热度分"):
        out[c] = pd.to_numeric(out[c], errors="coerce")   # None 混 float → object dtype
    out = out.sort_values("热度分", ascending=False, na_position="last")
    out.attrs["index_available"] = mom.attrs.get("index_available", True)
    out.attrs["weights"] = {W_EXCESS: w_ex, W_NET: w_net}
    return out
