"""定时清理：删除超出窗口（过去 N 个交易日）的新闻/公告/股吧数据。

采集/打分只在用户访问时写入；删除统一由本脚本通过定时任务执行，
避免每次浏览都触发删库。建议每日运行一次（见 README「定时执行」）。

    python purge_news.py
"""

from data.news import purge_expired_news
from utils.logger import get_logger

logger = get_logger("purge_news")


if __name__ == "__main__":
    deleted = purge_expired_news()
    logger.info(f"新闻清理任务完成：共删除 {deleted} 条窗口外数据")
