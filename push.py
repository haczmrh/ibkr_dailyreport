import requests
import os
from dotenv import load_dotenv
import logging

# 加载环境变量
load_dotenv()

def send_bark_notification(title, message):
    """发送 Bark 推送通知"""
    try:
        bark_url = os.getenv('BARK_URL')
        if not bark_url:
            logging.error("未找到 Bark URL 配置")
            return
            
        # 构建请求参数
        params = {
            'title': title,
            'body': message,
            'sound': 'minuet'  # 可选的通知声音
        }
        
        # 发送请求
        response = requests.get(bark_url, params=params)
        response.raise_for_status()
        
        logging.info(f"Bark 通知发送成功: {title} | 内容: {message}")
    except Exception as e:
        logging.error(f"发送 Bark 通知失败: {str(e)}") 