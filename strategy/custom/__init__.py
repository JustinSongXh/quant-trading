"""AI 生成 / 手写的自定义交易策略存放目录。

本目录下**非下划线开头**的 .py 会被 strategy.trade_strategy._discover_custom()
自动发现并注册；每个文件恰好一个 TradeStrategy 子类，且 key == 文件名。

_staging/  待校验，不被发现
_rejected/ 校验失败归档
"""
