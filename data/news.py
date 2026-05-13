"""新闻数据采集（预留）"""

import akshare as ak
import pandas as pd


def fetch_stock_news(symbol: str, limit: int = 50) -> pd.DataFrame:
    """获取个股相关新闻

    TODO: 待实现，数据源待确定（AKShare 财经新闻接口 / 东方财富）
    """
    raise NotImplementedError("新闻采集模块待实现")
