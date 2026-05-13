"""微信通知模块（通过 PushPlus 推送）

使用方法：
1. 微信扫码关注 PushPlus 公众号：https://www.pushplus.plus/
2. 获取 token，填入 .env 文件的 PUSHPLUS_TOKEN
"""

import requests
from config.settings import PUSHPLUS_TOKEN
from utils.logger import get_logger

logger = get_logger("notify")

PUSHPLUS_API = "http://www.pushplus.plus/send"


def send_wechat(title: str, content: str) -> bool:
    """发送微信通知

    Args:
        title: 消息标题
        content: 消息内容（支持 HTML）

    Returns:
        是否发送成功
    """
    if not PUSHPLUS_TOKEN:
        logger.warning("PUSHPLUS_TOKEN 未配置，跳过微信通知")
        return False

    data = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "html",
    }

    try:
        resp = requests.post(PUSHPLUS_API, json=data, timeout=10)
        result = resp.json()
        if result.get("code") == 200:
            logger.info(f"微信通知发送成功: {title}")
            return True
        else:
            logger.error(f"微信通知发送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"微信通知异常: {e}")
        return False
