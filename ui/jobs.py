"""进程内定时任务（issue #42 从 app.py 拆出）"""

import streamlit as st


@st.cache_resource
def start_background_jobs():
    """进程内定时任务：每日清理窗口外新闻 + 收盘后落一份板块资金流快照。

    cache_resource 保证整个 app 进程只起一次（Streamlit 每次交互都会重跑脚本，
    普通模块级代码会重复执行；缓存资源不会）。容器 TZ=Asia/Shanghai，按本地时间触发。

    资金流快照（#34）：资金流没有逐日历史接口，只能自己攒。15:40 跑（A 股 15:00 收盘，
    留 40 分钟等 THS 数据稳定），工作日触发、非交易日由 snapshot_fund_flow 内部的交易日
    闸门挡掉。本仓库目前没有任何功能读这张表——今天不开始积累，将来想用就得再等几个月。

    成分股夜间刷新（#35）：东财成分股接口是**窗口性限流**（实测同一分钟内 22 个
    (分片,协议) 组合只有 2 个通，两分钟后全挂，再过 40 秒又活）。白天用户点「选股」时
    只肯等一轮，所以缓存得靠夜里攒——每晚 03:20 跑一遍池内板块，函数内部每页最多重试
    JOB_ATTEMPTS 轮，一晚下来大概率能撞上可用窗口。每天跑而不是按 7 天 TTL 跑：白跑一晚
    只是几十个 HTTP 请求，而缓存空着会让第二天的选股入口全都要现场赌限流。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from data.news import purge_expired_news
    from data.fund_flow import snapshot_fund_flow
    from data.sector_cons import refresh_pool_constituents

    sched = BackgroundScheduler()
    sched.add_job(purge_expired_news, "cron", hour=3, minute=0,
                  id="purge_expired_news", replace_existing=True)
    sched.add_job(snapshot_fund_flow, "cron", day_of_week="mon-fri",
                  hour=15, minute=40,
                  id="snapshot_sector_fund_flow", replace_existing=True)
    sched.add_job(refresh_pool_constituents, "cron", hour=3, minute=20,
                  id="refresh_sector_constituents", replace_existing=True)
    sched.start()
    return sched
