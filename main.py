"""入口：运行回测流程"""

from config.settings import STOCK_POOL
from data.fetcher import fetch_daily_kline
from data.mock import fetch_mock_kline
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

        # 1. 获取数据（优先读缓存 → AKShare → Mock）
        df = load_kline(symbol)
        if df is None:
            try:
                logger.info(f"  Fetching from AKShare...")
                df = fetch_daily_kline(symbol)
                save_kline(symbol, df)
                logger.info(f"  Cached {len(df)} rows")
            except Exception as e:
                logger.warning(f"  AKShare failed ({e}), using mock data")
                df = fetch_mock_kline(symbol)

        # 2. 构建信号（技术 + 缠论，情绪暂不接入）
        signal_df = build_signals(df, symbol=symbol, sentiment_scores=None)

        # 3. 融合决策
        fusion_result = fuse_signals(signal_df)

        # 4. 回测
        result = run_backtest(symbol, signal_df, fusion_result)

        # 5. 计算绩效
        metrics = calc_metrics(result["net_values"], result["initial_capital"])
        result["metrics"] = metrics
        results.append(result)

        logger.info(f"  {symbol} done: {metrics}")

    return results


if __name__ == "__main__":
    run()
