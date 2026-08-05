"""盘后信号扫描：收盘后运行，扫描股票池，把当日有买卖信号的标的打到终端。

**没有推送**：原先这里会把结果拼成 HTML 发 PushPlus 微信，但那条链路一次都没生效过
（`.env` 从未配 token，也没有 crontab 在调本脚本），#4 起删掉——要做通知就连调度一起做，
留一个空转的出口只会让人以为收盘后有推送在跑。

    docker exec quant-app python scanner.py
"""

from datetime import datetime
from config.pool import load_stock_pool
from config.settings import DEFAULT_SIGNAL_SOURCE
from data.fetcher import fetch_daily_kline
from data.cache import save_kline, load_kline
from strategy.signals import build_signals
from strategy.decision import decide, SOURCE_COLUMNS
from utils.logger import get_logger

logger = get_logger("scanner")


def scan_all() -> list[dict]:
    """扫描全部股票池，返回今日有信号的股票列表"""
    stocks = load_stock_pool()
    alerts = []

    for stock in stocks:
        code = stock["code"]
        name = stock["name"]
        stype = stock.get("type", "stock")
        cache_key = f"idx_{code}" if stype == "index" else code
        logger.info(f"Scanning {name}({code})...")

        try:
            # 获取数据（先尝试更新，失败则用缓存）
            try:
                df = fetch_daily_kline(code, stock_type=stype)
                save_kline(cache_key, df)
            except Exception as e:
                logger.warning(f"  fetch failed ({e}), trying cache...")
                df = load_kline(cache_key)
                if df is None:
                    logger.error(f"  {name}({code}): no data available, skip")
                    continue

            # 构建信号（按默认信号源）
            source = DEFAULT_SIGNAL_SOURCE
            signal_df = build_signals(df, symbol=code, sentiment_scores=None,
                                       enabled_signals=[source])
            fusion_result = decide(signal_df, source=source)

            # 取最后一个交易日的信号
            if len(fusion_result) == 0:
                continue

            today_decision = fusion_result["decision"].iloc[-1]
            today_strength = fusion_result["strength"].iloc[-1]
            today_date = str(signal_df.index[-1].date())
            today_close = signal_df.iloc[-1]["close"]
            today_signal = float(signal_df.iloc[-1][SOURCE_COLUMNS[source]])

            if today_decision != 0:
                action = "买入" if today_decision == 1 else "卖出"
                # 仓位建议
                if action == "买入":
                    from config.settings import POSITION_SIZING
                    pos_label = POSITION_SIZING[-1]["label"]
                    for tier in POSITION_SIZING:
                        if today_strength >= tier["min_strength"]:
                            pos_label = tier["label"]
                            break
                else:
                    pos_label = "清仓"
                alert = {
                    "code": code,
                    "name": name,
                    "date": today_date,
                    "action": action,
                    "close": today_close,
                    "strength": round(today_strength, 3),
                    "position": pos_label,
                    "source": source,
                    "signal": round(today_signal, 3),
                }
                alerts.append(alert)
                logger.info(f"  >>> {name} {action} 信号({pos_label})! 信号值={today_signal:.3f} 收盘价={today_close:.2f}")
            else:
                logger.info(f"  {name}: 无信号")

        except Exception as e:
            logger.error(f"  {name}({code}) scan error: {e}")

    return alerts


def run():
    """执行扫描并把结果打到终端，返回 alerts（供别的脚本复用）"""
    logger.info("=" * 50)
    logger.info("盘后信号扫描开始")
    logger.info("=" * 50)

    alerts = scan_all()
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\n📊 {today} 盘后扫描：共 {len(alerts)} 个信号")
    for a in alerts:
        print(f"  {a['action']} | {a['name']}({a['code']}) | 收盘:{a['close']:.2f} "
              f"| {a['source']}信号:{a['signal']} | {a['position']}")
    if not alerts:
        print("  今日无信号")
    print("  信号值范围 -1(强卖)~+1(强买)；缠论 B1=1.0 B2=0.7 B3=0.8 S1=-1.0 S2=-0.7 S3=-0.8")

    logger.info(f"扫描完成，共 {len(alerts)} 个信号")
    return alerts


if __name__ == "__main__":
    run()
