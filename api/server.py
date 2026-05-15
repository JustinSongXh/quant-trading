"""FastAPI 后端 API：为小程序提供信号数据"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from config.settings import load_stock_pool, get_stock_name, POSITION_SIZING
from data.fetcher import fetch_daily_kline, is_hk_stock
from data.cache import save_kline, load_kline, is_cache_fresh
from strategy.signals import build_signals
from strategy.fusion import fuse_signals
from analysis.chanlun import analyze as chanlun_analyze

app = FastAPI(title="量化分析 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_data(code: str, days: int = 400):
    """获取股票数据，优先缓存"""
    if is_cache_fresh(code):
        df = load_kline(code)
        if df is not None and len(df) > 0:
            return df
    df = fetch_daily_kline(code, days=days)
    save_kline(code, df)
    return df


def _get_position_label(strength: float) -> str:
    for tier in POSITION_SIZING:
        if strength >= tier["min_strength"]:
            return tier["label"]
    return POSITION_SIZING[-1]["label"]


@app.get("/api/overview")
def overview():
    """全局信号总览"""
    stocks = load_stock_pool()
    results = []

    for stock in stocks:
        code, name = stock["code"], stock["name"]
        market = stock.get("market", "A")
        try:
            df = _get_data(code)
            signal_df = build_signals(df, symbol=code, sentiment_scores=None,
                                       enabled_signals=["technical"])
            fusion_result = fuse_signals(signal_df, source="technical")

            last = signal_df.iloc[-1]
            decision = int(fusion_result["decision"].iloc[-1])
            strength = float(fusion_result["strength"].iloc[-1])

            rec = "买入" if decision == 1 else "卖出" if decision == -1 else "观望"
            pos_label = _get_position_label(strength) if decision != 0 else ""

            results.append({
                "code": code,
                "name": name,
                "market": market,
                "close": round(float(last["close"]), 2),
                "technical_signal": round(float(last.get("technical_signal", 0)), 3),
                "chanlun_signal": round(float(last.get("chanlun_signal", 0)), 3),
                "recommendation": rec,
                "strength": round(strength, 3),
                "position": pos_label,
                "date": str(signal_df.index[-1].date()),
            })
        except Exception as e:
            results.append({
                "code": code, "name": name, "market": market,
                "close": 0, "technical_signal": 0, "chanlun_signal": 0,
                "recommendation": "数据异常", "strength": 0, "position": "",
                "date": "", "error": str(e),
            })

    return {"stocks": results}


@app.get("/api/detail/{code}")
def detail(code: str, days: int = 365):
    """单股票详细分析"""
    try:
        name = get_stock_name(code)
        df = _get_data(code, days=max(days + 60, 400))

        # 按时间截取
        import pandas as pd
        cutoff = df.index.max() - pd.Timedelta(days=days)
        df = df.loc[df.index >= cutoff]

        signal_df = build_signals(df, symbol=code, sentiment_scores=None,
                                   enabled_signals=["technical"])
        fusion_result = fuse_signals(signal_df, source="technical")
        chan_result = chanlun_analyze(df, code)

        last = signal_df.iloc[-1]
        decision = int(fusion_result["decision"].iloc[-1])
        strength = float(fusion_result["strength"].iloc[-1])
        rec = "买入" if decision == 1 else "卖出" if decision == -1 else "观望"
        pos_label = _get_position_label(strength) if decision != 0 else ""

        # K线数据（供 echarts 渲染）
        kline_data = []
        for dt, row in signal_df.iterrows():
            kline_data.append({
                "date": str(dt.date()),
                "open": round(float(row["open"]), 2),
                "close": round(float(row["close"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "volume": int(row.get("volume", 0)),
                "ma5": round(float(row.get("ma_5", 0)), 2) if not pd.isna(row.get("ma_5")) else None,
                "ma20": round(float(row.get("ma_20", 0)), 2) if not pd.isna(row.get("ma_20")) else None,
                "macd_dif": round(float(row.get("macd_dif", 0)), 3) if not pd.isna(row.get("macd_dif")) else None,
                "macd_dea": round(float(row.get("macd_dea", 0)), 3) if not pd.isna(row.get("macd_dea")) else None,
                "macd_hist": round(float(row.get("macd_hist", 0)), 3) if not pd.isna(row.get("macd_hist")) else None,
                "rsi": round(float(row.get("rsi", 0)), 1) if not pd.isna(row.get("rsi")) else None,
                "st_direction": int(row.get("st_direction", 0)) if not pd.isna(row.get("st_direction")) else None,
                "st_value": round(float(row.get("st_value", 0)), 2) if not pd.isna(row.get("st_value")) else None,
            })

        # 策略判定
        strategies = {
            "macd": {
                "dif": round(float(last.get("macd_dif", 0)), 3),
                "dea": round(float(last.get("macd_dea", 0)), 3),
                "hist": round(float(last.get("macd_hist", 0)), 3),
                "verdict": "多头" if last.get("macd_dif", 0) > last.get("macd_dea", 0) else "空头",
            },
            "rsi": {
                "value": round(float(last.get("rsi", 50)), 1),
                "verdict": "超卖" if last.get("rsi", 50) < 30 else "超买" if last.get("rsi", 50) > 70 else "中性",
            },
            "supertrend": {
                "direction": "多头" if last.get("st_direction", 0) == 1 else "空头",
                "value": round(float(last.get("st_value", 0)), 2),
            },
            "chanlun": {
                "bi_count": len(chan_result["bi_list"]),
                "zs_count": len(chan_result["zs_list"]),
                "buy_count": len(chan_result["buy_points"]),
                "sell_count": len(chan_result["sell_points"]),
            },
        }

        # 缠论买卖点
        chan_points = []
        for bp in chan_result["buy_points"]:
            chan_points.append({"date": str(bp["dt"].date()), "type": bp["type"], "direction": "买", "price": round(bp["price"], 2)})
        for sp in chan_result["sell_points"]:
            chan_points.append({"date": str(sp["dt"].date()), "type": sp["type"], "direction": "卖", "price": round(sp["price"], 2)})

        return {
            "code": code,
            "name": name,
            "market": "HK" if is_hk_stock(code) else "A",
            "date": str(signal_df.index[-1].date()),
            "close": round(float(last["close"]), 2),
            "recommendation": rec,
            "strength": round(strength, 3),
            "position": pos_label,
            "technical_signal": round(float(last.get("technical_signal", 0)), 3),
            "chanlun_signal": round(float(last.get("chanlun_signal", 0)), 3),
            "strategies": strategies,
            "chan_points": chan_points,
            "kline": kline_data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stocks")
def stock_list():
    """获取股票池"""
    return {"stocks": load_stock_pool()}
