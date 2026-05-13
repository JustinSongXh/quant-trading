"""缠论分析模块：基于 czsc 库识别笔、线段、中枢，生成买卖点信号"""

import pandas as pd
import numpy as np
from datetime import timedelta
from czsc import CZSC, RawBar, Freq, Direction
from utils.logger import get_logger

logger = get_logger("chanlun")


def kline_to_bars(df: pd.DataFrame, symbol: str = "stock") -> list[RawBar]:
    """将 DataFrame K线数据转为 czsc RawBar 列表"""
    bars = []
    for i, (dt, row) in enumerate(df.iterrows()):
        bars.append(RawBar(
            symbol=symbol, id=i, dt=dt.to_pydatetime(),
            freq=Freq.D,
            open=float(row["open"]),
            close=float(row["close"]),
            high=float(row["high"]),
            low=float(row["low"]),
            vol=float(row.get("volume", 0)),
            amount=float(row.get("turnover", 0)),
        ))
    return bars


def analyze(df: pd.DataFrame, symbol: str = "stock") -> dict:
    """对K线数据进行缠论分析

    Returns:
        dict 包含:
        - czsc: CZSC 对象
        - bi_list: 笔列表
        - zs_list: 中枢列表（从笔推导）
        - buy_points: 买点列表
        - sell_points: 卖点列表
    """
    bars = kline_to_bars(df, symbol)
    c = CZSC(bars)

    bi_list = c.bi_list
    zs_list = _find_zhongshu(bi_list)
    buy_points, sell_points = _find_bs_points(bi_list, zs_list)

    logger.info(f"  {symbol}: {len(c.fx_list)} fx, {len(bi_list)} bi, {len(zs_list)} zs, "
                f"{len(buy_points)} buys, {len(sell_points)} sells")

    return {
        "czsc": c,
        "bi_list": bi_list,
        "zs_list": zs_list,
        "buy_points": buy_points,
        "sell_points": sell_points,
    }


def _find_zhongshu(bi_list: list) -> list[dict]:
    """从笔列表中识别中枢

    中枢定义：至少3笔有重叠区间
    """
    if len(bi_list) < 3:
        return []

    zs_list = []
    i = 0
    while i < len(bi_list) - 2:
        bi1, bi2, bi3 = bi_list[i], bi_list[i + 1], bi_list[i + 2]

        # 三笔重叠区间
        zs_high = min(bi1.high, bi2.high, bi3.high)
        zs_low = max(bi1.low, bi2.low, bi3.low)

        if zs_low < zs_high:
            # 找到中枢，尝试延伸
            zs = {
                "high": zs_high,
                "low": zs_low,
                "start_dt": bi1.sdt,
                "end_dt": bi3.edt,
                "bis": [bi1, bi2, bi3],
                "bi_count": 3,
            }
            j = i + 3
            while j < len(bi_list):
                bi_next = bi_list[j]
                # 新笔与中枢有重叠则延伸
                if bi_next.low < zs_high and bi_next.high > zs_low:
                    zs["end_dt"] = bi_next.edt
                    zs["bis"].append(bi_next)
                    zs["bi_count"] += 1
                    j += 1
                else:
                    break
            zs_list.append(zs)
            i = j
        else:
            i += 1

    return zs_list


def _find_bs_points(bi_list: list, zs_list: list[dict]) -> tuple[list[dict], list[dict]]:
    """识别三类买卖点

    一买：下跌趋势末端背驰（最后一笔力度小于前一笔同向笔）
    二买：一买后回调不破前低
    三买：离开中枢后回踩不回中枢

    一卖/二卖/三卖对称处理
    """
    buy_points = []
    sell_points = []

    if len(bi_list) < 5:
        return buy_points, sell_points

    # === 一类买卖点：背驰 ===
    for i in range(4, len(bi_list)):
        bi = bi_list[i]
        # 找前一根同方向的笔
        prev_same = None
        for j in range(i - 2, -1, -2):
            if bi_list[j].direction == bi.direction:
                prev_same = bi_list[j]
                break
        if prev_same is None:
            continue

        # 一买：向下笔，当前笔创新低但力度（power_price）减弱
        if bi.direction == Direction.Down and bi.low < prev_same.low:
            if bi.power_price < prev_same.power_price:
                buy_points.append({
                    "type": "B1",
                    "dt": bi.edt,
                    "price": bi.low,
                    "reason": f"下跌背驰, power {bi.power_price:.2f} < {prev_same.power_price:.2f}",
                })

        # 一卖：向上笔，当前笔创新高但力度减弱
        if bi.direction == Direction.Up and bi.high > prev_same.high:
            if bi.power_price < prev_same.power_price:
                sell_points.append({
                    "type": "S1",
                    "dt": bi.edt,
                    "price": bi.high,
                    "reason": f"上涨背驰, power {bi.power_price:.2f} < {prev_same.power_price:.2f}",
                })

    # === 二类买卖点：回调不破前低/不破前高 ===
    for bp in list(buy_points):
        if bp["type"] != "B1":
            continue
        # 找 B1 之后的第一根向下笔
        b1_dt = bp["dt"]
        for bi in bi_list:
            if bi.sdt >= b1_dt and bi.direction == Direction.Down:
                if bi.low > bp["price"]:
                    buy_points.append({
                        "type": "B2",
                        "dt": bi.edt,
                        "price": bi.low,
                        "reason": f"回调不破前低 {bp['price']:.2f}",
                    })
                break

    for sp in list(sell_points):
        if sp["type"] != "S1":
            continue
        s1_dt = sp["dt"]
        for bi in bi_list:
            if bi.sdt >= s1_dt and bi.direction == Direction.Up:
                if bi.high < sp["price"]:
                    sell_points.append({
                        "type": "S2",
                        "dt": bi.edt,
                        "price": bi.high,
                        "reason": f"反弹不破前高 {sp['price']:.2f}",
                    })
                break

    # === 三类买卖点：中枢突破回踩 ===
    for zs in zs_list:
        zs_end = zs["end_dt"]
        after_bis = [bi for bi in bi_list if bi.sdt >= zs_end]

        if len(after_bis) < 2:
            continue

        first_out = after_bis[0]
        pullback = after_bis[1]

        # 三买：向上离开中枢后回踩不破中枢上沿
        if first_out.direction == Direction.Up and first_out.high > zs["high"]:
            if pullback.direction == Direction.Down and pullback.low > zs["high"]:
                buy_points.append({
                    "type": "B3",
                    "dt": pullback.edt,
                    "price": pullback.low,
                    "reason": f"中枢[{zs['low']:.2f},{zs['high']:.2f}]突破回踩",
                })

        # 三卖：向下离开中枢后反弹不破中枢下沿
        if first_out.direction == Direction.Down and first_out.low < zs["low"]:
            if pullback.direction == Direction.Up and pullback.high < zs["low"]:
                sell_points.append({
                    "type": "S3",
                    "dt": pullback.edt,
                    "price": pullback.high,
                    "reason": f"中枢[{zs['low']:.2f},{zs['high']:.2f}]跌破反弹",
                })

    return buy_points, sell_points


def generate_chanlun_signals(df: pd.DataFrame, symbol: str = "stock") -> pd.Series:
    """生成缠论买卖点信号

    Returns:
        Series: -1 ~ +1, 以日期为 index
        买点信号强度：B1=1.0, B2=0.7, B3=0.8
        卖点信号强度：S1=-1.0, S2=-0.7, S3=-0.8
    """
    result = analyze(df, symbol)
    signals = pd.Series(0.0, index=df.index)

    strength = {"B1": 1.0, "B2": 0.7, "B3": 0.8, "S1": -1.0, "S2": -0.7, "S3": -0.8}

    for bp in result["buy_points"]:
        dt = bp["dt"]
        # 找最近的交易日
        idx = df.index.searchsorted(dt)
        if idx < len(df.index):
            signals.iloc[idx] = max(signals.iloc[idx], strength.get(bp["type"], 0.5))

    for sp in result["sell_points"]:
        dt = sp["dt"]
        idx = df.index.searchsorted(dt)
        if idx < len(df.index):
            signals.iloc[idx] = min(signals.iloc[idx], strength.get(sp["type"], -0.5))

    return signals
