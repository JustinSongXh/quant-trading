"""入口：运行回测流程"""

from config.settings import STOCK_POOL
from data.fetcher import fetch_daily_kline
from data.cache import save_kline, load_kline
from strategy.signals import build_signals
from strategy.fusion import fuse_signals
from backtest.engine import run_backtest
from backtest.metrics import calc_metrics
from utils.logger import get_logger

logger = get_logger()


def run(symbols: list[str] | None = None):
    symbols = symbols or STOCK_POOL
    results = []

    for symbol in symbols:
        logger.info(f"Processing {symbol}...")

        # 1. 获取数据（优先读缓存）
        df = load_kline(symbol)
        if df is None:
            logger.info(f"  Fetching from AKShare...")
            df = fetch_daily_kline(symbol)
            save_kline(symbol, df)
            logger.info(f"  Cached {len(df)} rows")

        # 2. 构建信号（情绪信号暂不接入）
        signal_df = build_signals(df, sentiment_scores=None)

        # 3. 融合决策
        decisions = fuse_signals(signal_df)

        # 4. 回测
        result = run_backtest(symbol, signal_df, decisions)

        # 5. 计算绩效
        metrics = calc_metrics(result["net_values"], result["initial_capital"])
        result["metrics"] = metrics
        results.append(result)

        logger.info(f"  {symbol} done: {metrics}")

    return results


if __name__ == "__main__":
    run()
