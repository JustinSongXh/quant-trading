"""Kronos 模型预测测试脚本

在 Docker 容器中运行：
  docker exec quant-app bash -c "cd /app && bash tests/setup_kronos.sh && python tests/test_kronos.py"

测试内容：用 Kronos 预测沪深300未来5天走势
"""

import sys
import os
import time

# 将克隆的 Kronos 代码加入 path
sys.path.insert(0, "/app/tests/kronos_repo")

import pandas as pd
import akshare as ak


def fetch_test_data(symbol="000300", days=500):
    """获取沪深300日K线作为测试数据"""
    print(f"[1/4] 获取 {symbol} 近 {days} 天日K线数据...")
    df = ak.stock_zh_index_daily(symbol=f"sh{symbol}")
    df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                             "high": "high", "low": "low", "volume": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # 只取最近 days 天
    df = df.tail(days).reset_index(drop=True)

    # amount 列：有则用，无则用 close*volume 估算
    if "amount" not in df.columns:
        df["amount"] = df["close"] * df["volume"]

    print(f"  数据范围: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}, 共 {len(df)} 行")
    return df


def run_prediction(df, pred_days=5):
    """用 Kronos 模型预测未来 N 天"""
    from model import Kronos, KronosTokenizer, KronosPredictor

    print("[2/4] 加载 Kronos 模型（首次需下载，约 400MB）...")
    t0 = time.time()
    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    print(f"  模型加载耗时: {time.time() - t0:.1f}s")

    lookback = min(400, len(df))

    x_df = df.iloc[-lookback:][["open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
    x_timestamp = pd.Series(df.iloc[-lookback:]["date"].values).reset_index(drop=True)

    last_date = df["date"].iloc[-1]
    y_timestamp = pd.Series(pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=pred_days))

    print(f"[3/4] 预测未来 {pred_days} 个交易日（输入 {lookback} 天历史数据）...")
    t0 = time.time()
    pred_df = predictor.predict(
        df=x_df,
        x_timestamp=x_timestamp,
        y_timestamp=y_timestamp,
        pred_len=pred_days,
        T=1.0,
        top_p=0.9,
        sample_count=1,
    )
    print(f"  预测耗时: {time.time() - t0:.1f}s")

    return pred_df, last_date


def show_results(df, pred_df, last_date, pred_days):
    """展示预测结果"""
    print("\n[4/4] 预测结果")
    print("=" * 60)

    last_close = df["close"].iloc[-1]
    print(f"最后交易日: {last_date.date()}, 收盘价: {last_close:.2f}")
    print()

    pred_df = pred_df.reset_index(drop=True)
    y_dates = pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=pred_days)

    print(f"{'日期':>12s}  {'预测开盘':>8s}  {'预测最高':>8s}  {'预测最低':>8s}  {'预测收盘':>8s}  {'涨跌幅':>7s}")
    print("-" * 60)

    prev = last_close
    for i in range(len(pred_df)):
        o = pred_df.at[i, "open"]
        h = pred_df.at[i, "high"]
        l = pred_df.at[i, "low"]
        c = pred_df.at[i, "close"]
        chg = (c - prev) / prev * 100
        date_str = y_dates[i].strftime("%Y-%m-%d")
        print(f"{date_str:>12s}  {o:>8.2f}  {h:>8.2f}  {l:>8.2f}  {c:>8.2f}  {chg:>+6.2f}%")
        prev = c

    total_chg = (pred_df.at[len(pred_df) - 1, "close"] - last_close) / last_close * 100
    print("-" * 60)
    print(f"未来 {pred_days} 天累计预测涨跌: {total_chg:+.2f}%")

    if total_chg > 1:
        signal = "看涨 (bullish)"
    elif total_chg < -1:
        signal = "看跌 (bearish)"
    else:
        signal = "震荡 (neutral)"
    print(f"信号判断: {signal}")


if __name__ == "__main__":
    pred_days = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    df = fetch_test_data()
    pred_df, last_date = run_prediction(df, pred_days=pred_days)
    show_results(df, pred_df, last_date, pred_days)
