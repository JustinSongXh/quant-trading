"""盘后信号扫描：每日收盘后运行，扫描股票池，发现买卖信号推送微信"""

import sys
from datetime import datetime
from config.settings import load_stock_pool, get_stock_name
from data.fetcher import fetch_daily_kline
from data.cache import save_kline, load_kline
from strategy.signals import build_signals
from strategy.fusion import fuse_signals
from notify.wechat import send_wechat
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

            # 构建信号
            signal_df = build_signals(df, symbol=code, sentiment_scores=None)
            fusion_result = fuse_signals(signal_df)

            # 取最后一个交易日的信号
            if len(fusion_result) == 0:
                continue

            today_decision = fusion_result["decision"].iloc[-1]
            today_strength = fusion_result["strength"].iloc[-1]
            today_date = str(signal_df.index[-1].date())
            today_close = signal_df.iloc[-1]["close"]
            today_tech = signal_df.iloc[-1]["technical_signal"]
            today_chan = signal_df.iloc[-1]["chanlun_signal"]

            if today_decision != 0:
                action = "买入" if today_decision == 1 else "卖出"
                # 仓位建议
                from config.settings import POSITION_SIZING
                pos_label = POSITION_SIZING[-1]["label"]
                for tier in POSITION_SIZING:
                    if today_strength >= tier["min_strength"]:
                        pos_label = tier["label"]
                        break
                alert = {
                    "code": code,
                    "name": name,
                    "date": today_date,
                    "action": action,
                    "close": today_close,
                    "strength": round(today_strength, 3),
                    "position": pos_label,
                    "technical_signal": round(today_tech, 3),
                    "chanlun_signal": round(today_chan, 3),
                }
                alerts.append(alert)
                logger.info(f"  >>> {name} {action} 信号({pos_label})! 强度={today_strength:.3f} 收盘价={today_close:.2f}")
            else:
                logger.info(f"  {name}: 无信号")

        except Exception as e:
            logger.error(f"  {name}({code}) scan error: {e}")

    return alerts


def format_report(alerts: list[dict]) -> str:
    """将信号列表格式化为 HTML 报告"""
    today = datetime.now().strftime("%Y-%m-%d")

    if not alerts:
        return f"""
        <h3>📊 {today} 盘后扫描报告</h3>
        <p>今日股票池无买卖信号。</p>
        """

    rows = ""
    for a in alerts:
        color = "#e74c3c" if a["action"] == "买入" else "#27ae60"
        rows += f"""
        <tr>
            <td>{a['name']}({a['code']})</td>
            <td style="color:{color};font-weight:bold">{a['action']}</td>
            <td>{a['close']:.2f}</td>
            <td>{a['technical_signal']}</td>
            <td>{a['chanlun_signal']}</td>
        </tr>
        """

    return f"""
    <h3>📊 {today} 盘后扫描报告</h3>
    <p>共发现 <b>{len(alerts)}</b> 个信号：</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
        <tr style="background:#f0f0f0">
            <th>股票</th><th>操作</th><th>收盘价</th><th>技术信号</th><th>缠论信号</th>
        </tr>
        {rows}
    </table>
    <br>
    <p style="color:#999;font-size:12px">
        技术信号: -1(强卖)~+1(强买) | 缠论信号: B1=1.0 B2=0.7 B3=0.8 S1=-1.0 S2=-0.7 S3=-0.8
    </p>
    """


def run():
    """执行扫描并推送"""
    logger.info("=" * 50)
    logger.info("盘后信号扫描开始")
    logger.info("=" * 50)

    alerts = scan_all()

    # 生成报告
    report = format_report(alerts)
    today = datetime.now().strftime("%Y-%m-%d")

    if alerts:
        title = f"🔔 {today} 发现 {len(alerts)} 个交易信号"
    else:
        title = f"📊 {today} 盘后扫描：无信号"

    # 推送微信
    send_wechat(title, report)

    # 同时打印到终端
    for a in alerts:
        print(f"  {a['action']} | {a['name']}({a['code']}) | 收盘:{a['close']:.2f} "
              f"| 技术:{a['technical_signal']} | 缠论:{a['chanlun_signal']}")

    if not alerts:
        print("  今日无信号")

    logger.info(f"扫描完成，共 {len(alerts)} 个信号")
    return alerts


if __name__ == "__main__":
    run()
