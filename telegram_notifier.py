# telegram_notifier.py
import requests
import os
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

def send_telegram_notification(title, body_message):
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_ids_str = os.getenv('TELEGRAM_CHAT_IDS') # 读取新的环境变量

    if not bot_token:
        logger.error("未找到 Telegram Bot Token (TELEGRAM_BOT_TOKEN)，无法发送通知。")
        return
    if not chat_ids_str:
        logger.error("未找到 Telegram Chat IDs (TELEGRAM_CHAT_IDS)，无法发送通知。")
        return

    chat_ids = [chat_id.strip() for chat_id in chat_ids_str.split(',')] # 分割字符串并去除多余空格

    full_message = f"{title}\n{body_message}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    success_count = 0
    failure_count = 0

    for chat_id in chat_ids:
        if not chat_id: # 跳过空的 chat_id (例如，如果末尾有逗号)
            continue

        payload_plain = {
            'chat_id': chat_id,
            'text': full_message
        }

        try:
            response = requests.post(url, data=payload_plain, timeout=10)
            response.raise_for_status()
            response_json = response.json()
            if response_json.get("ok"):
                logger.info(f"Telegram 通知已成功发送到 Chat ID: {chat_id}。标题: {title}")
                success_count += 1
            else:
                logger.error(f"发送 Telegram 通知到 Chat ID: {chat_id} 失败: Telegram API 返回 'ok: false'。响应: {response_json.get('description', '无描述')}")
                failure_count += 1
        except requests.exceptions.Timeout:
            logger.error(f"发送 Telegram 通知到 Chat ID: {chat_id} 超时。")
            failure_count += 1
        except requests.exceptions.HTTPError as e:
            logger.error(f"发送 Telegram 通知到 Chat ID: {chat_id} 时发生 HTTP 错误: {e.response.status_code} - {e.response.text}")
            failure_count += 1
        except Exception as e:
            logger.error(f"发送 Telegram 通知到 Chat ID: {chat_id} 时发生未知错误: {str(e)}", exc_info=True)
            failure_count += 1

    if failure_count > 0:
        logger.warning(f"Telegram 通知发送完成，成功: {success_count}，失败: {failure_count}。")
    elif success_count > 0:
        logger.info(f"所有 Telegram 通知 ({success_count}个) 均已成功发送。")