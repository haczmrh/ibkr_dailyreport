# 文件: ibkr_net_value_tracker.py (最终完整版)

import requests
import time
from datetime import datetime, timedelta, date
import os
from dotenv import load_dotenv
import logging
import logging.handlers
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
import pytz
import json
from telegram_notifier import send_telegram_notification

# 添加当前目录到 Python 路径，确保 push 模块能被找到
sys.path.append(str(Path(__file__).resolve().parent))
from push import send_bark_notification

# 加载 .env 文件中的环境变量
load_dotenv()

# --- 配置日志记录 ---
log_dir = '/opt/ibkr_net_worth_tracker/logs'
os.makedirs(log_dir, exist_ok=True)
log_file_basename = 'ibkr_tracker.log'
log_file_path = os.path.join(log_dir, log_file_basename)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
timed_file_handler = logging.handlers.TimedRotatingFileHandler(
    log_file_path,
    when='midnight',
    interval=1,
    backupCount=30,
    encoding='utf-8'
)
timed_file_handler.setFormatter(formatter)
timed_file_handler.suffix = "%Y-%m-%d"
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(timed_file_handler)
logger.addHandler(stream_handler)
# --- 日志配置结束 ---

class IBKRTracker:
    def __init__(self):
        self.flex_token = os.getenv('IB_FLEX_TOKEN')
        self.query_id = os.getenv('IB_QUERY_ID')
        self.send_request_url = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
        self.get_statement_url = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
        
        self.et_timezone = pytz.timezone('US/Eastern')
        
        self.last_notified_raw_fromdate = None
        self.initial_run_for_notification = True
        self.hunt_active_for_current_cycle = False

        self.state_file_path = Path(__file__).resolve().parent / 'tracker_state.json'
        self.state = {}
        self.load_state()

    def load_state(self):
        """从 JSON 文件加载状态，并兼容旧结构"""
        try:
            if self.state_file_path.exists():
                with open(self.state_file_path, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
                    logging.info("成功加载 tracker_state.json 文件。")
            else:
                logging.info("tracker_state.json 文件不存在，将使用初始状态启动。")
                self.state = {}
            
            # 兼容性检查：确保新结构的关键字段存在
            if 'last_report_details' not in self.state:
                self.state['last_report_details'] = None
            if 'weekly_start_nav' not in self.state:
                self.state['weekly_start_nav'] = 0.0
            if 'monthly_start_nav' not in self.state:
                 self.state['monthly_start_nav'] = 0.0
            
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"加载 state 文件失败: {e}，将使用空的 state 继续。")
            self.state = {}
    
    def save_state(self):
        """将当前状态保存到 JSON 文件"""
        try:
            with open(self.state_file_path, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=4, default=str) # 使用default=str处理date对象
        except IOError as e:
            logging.error(f"保存 state 文件失败: {e}")

    def get_current_et_time(self):
        """获取当前美东时间"""
        return datetime.now(self.et_timezone)
        
    def get_account_summary(self):
        """获取账户摘要信息。"""
        try:
            params_send = {'t': self.flex_token, 'q': self.query_id, 'v': '3'}
            response_send = requests.get(self.send_request_url, params=params_send, timeout=30)
            response_send.raise_for_status()
            
            root_send = ET.fromstring(response_send.text)
            status_send = root_send.findtext('Status') 
            if status_send != 'Success':
                error_message = root_send.findtext('ErrorMessage', "发送请求返回状态非Success，但无ErrorMessage")
                logging.error(f"发送请求失败: {status_send} - {error_message}")
                return None
            
            reference_code = root_send.findtext('ReferenceCode')
            if not reference_code:
                logging.error("发送请求成功，但响应中未找到有效的 ReferenceCode。")
                return None
            
            time.sleep(30) 

            params_get = {'t': self.flex_token, 'q': reference_code, 'v': '3'}
            response_get = requests.get(self.get_statement_url, params=params_get, timeout=30)
            response_get.raise_for_status()
            
            xml_content = response_get.content
            try:
                root_get = ET.fromstring(xml_content) 
            except ET.ParseError as e_parse:
                problem_xml_text = xml_content.decode('utf-8', errors='replace')[:500]
                logging.error(f"XML 解析错误: {str(e_parse)}. 问题XML文本: {problem_xml_text}")
                return None

            error_code = root_get.findtext('ErrorCode')
            if error_code:
                error_message_get = root_get.findtext('ErrorMessage', "报告获取返回错误码，但无ErrorMessage")
                logging.error(f"获取报告时遇到问题: ErrorCode {error_code} - {error_message_get}")
                if error_code in ['1018', '1019', '1020']: 
                     logging.info("报告尚未准备好 (错误码 %s)，将由主循环重试。", error_code)
                return None

            statement = root_get.find('.//FlexStatements/FlexStatement')
            if statement is None:
                logging.error("响应XML中未找到 FlexStatement 节点。")
                return None
                
            raw_from_date_str = statement.get('fromDate') 
            report_date_display = self.get_current_et_time().strftime("%Y-%m-%d")
            if raw_from_date_str:
                report_date_display = f"{raw_from_date_str[:4]}-{raw_from_date_str[4:6]}-{raw_from_date_str[6:]}"

            change_in_nav = statement.find('ChangeInNAV')
            if change_in_nav is None:
                logging.error("未找到 ChangeInNAV 数据。")
                return None 
                
            account_details = {
                'startingValue': float(change_in_nav.get('startingValue', 0)),
                'endingValue': float(change_in_nav.get('endingValue', 0)),
                'mtm': float(change_in_nav.get('mtm', 0)),
                'depositsWithdrawals': float(change_in_nav.get('depositsWithdrawals', 0)),
                'reportDate': report_date_display, 
                'raw_from_date': raw_from_date_str 
            }
            logging.info(f"成功获取并解析账户数据，报告日期: {report_date_display}")
            return account_details
            
        except requests.exceptions.Timeout:
            logging.error("请求IBKR API超时。")
            return None
        except requests.exceptions.HTTPError as e:
            error_text = e.response.text[:200] if hasattr(e.response, 'text') else "无响应体" 
            status_code_info = e.response.status_code if hasattr(e.response, 'status_code') else "无状态码"
            logging.error(f"HTTP 错误: {status_code_info} - {error_text}")
            return None
        except Exception as e:
            logging.error(f"获取账户信息时发生未知错误: {str(e)}", exc_info=True)
            return None

    def _send_summary_notification(self, period_type, pl_value, date_obj):
        """负责发送格式化的总结通知"""
        verb = "上涨" if pl_value >= 0 else "下跌"
        
        if period_type == "week":
            title = "📈 本周总结"
            body = f"本周{verb}: ${pl_value:,.2f}"
        elif period_type == "month":
            title = f"🗓️ {date_obj.month}月总结"
            body = f"{date_obj.month}月{verb}: ${pl_value:,.2f}"
        else:
            return

        logging.info(f"准备发送总结通知: {title} | {body}")
        try:
            send_bark_notification(title, body)
            send_telegram_notification(title, body)
        except Exception as e:
            logging.error(f"发送总结通知失败: {e}", exc_info=True)

    def send_daily_report(self):
        """发送包含每日涨跌的日报，并独立判断是否需要发送即时的周/月总结报告"""
        details = self.get_account_summary()
        if details is None:
            return {'status': 'error_fetching'}
            
        if abs(details.get('mtm', 0)) < 0.01:
            logging.info(f"报告日期 {details['reportDate']} 无实质市值变动，认定为非交易日，跳过。")
            self.last_notified_raw_fromdate = details['raw_from_date']
            return {'status': 'skipped_non_trading_day'}

        current_raw_fromdate = details.get('raw_from_date')
        notify = False
        if self.initial_run_for_notification and current_raw_fromdate:
            notify = True
        elif current_raw_fromdate is not None and current_raw_fromdate != self.last_notified_raw_fromdate:
            notify = True
        else:
            logging.info(f"报告数据 (fromDate: {current_raw_fromdate}) 与上次已通知数据相同，跳过。")
            return {'status': 'no_notification_needed_duplicate'}

        if notify:
            # --- 1. 更新状态（必须在所有计算之前） ---
            current_date = datetime.strptime(details['reportDate'], '%Y-%m-%d').date()
            last_report = self.state.get('last_report_details')
            last_date = datetime.strptime(last_report['reportDate'], '%Y-%m-%d').date() if last_report else None
            
            is_new_week = (last_date is None or current_date.isocalendar()[1] != last_date.isocalendar()[1] or current_date.year != last_date.year)
            if is_new_week:
                self.state['weekly_start_nav'] = details['startingValue']
                self.state['weekly_deposits'] = 0.0
            
            is_new_month = (last_date is None or current_date.month != last_date.month or current_date.year != last_date.year)
            if is_new_month:
                self.state['monthly_start_nav'] = details['startingValue']
                self.state['monthly_deposits'] = 0.0

            self.state['weekly_deposits'] = self.state.get('weekly_deposits', 0) + details['depositsWithdrawals']
            self.state['monthly_deposits'] = self.state.get('monthly_deposits', 0) + details['depositsWithdrawals']

            # --- 2. 发送常规日报 ---
            self.last_notified_raw_fromdate = current_raw_fromdate
            if self.initial_run_for_notification:
                self.initial_run_for_notification = False

            net_change = details['mtm']
            change_text = "😴【一般】"
            if net_change > 1000:  change_text = "🥳【上头了】"
            elif net_change < -1000: change_text = "😩【狂泻】"
            change_display = "涨" if net_change >= 0 else "跌"
            
            title = f"{change_text} IB {details['reportDate']} 日报"
            
            message = f"{change_display}: ${net_change:,.2f}\n净资产: ${details['endingValue']:,.2f}"
            
            if details['depositsWithdrawals'] != 0:
                verb = "入金" if details['depositsWithdrawals'] > 0 else "出金"
                message += f"\n{verb}: ${abs(details['depositsWithdrawals']):,.2f}"

            logging.info(f"准备发送常规日报: {title} | {message.replace(chr(10), ' ')}")
            try:
                send_bark_notification(title, message)
                send_telegram_notification(title, message)
            except Exception as e:
                logging.error(f"发送常规日报失败: {e}", exc_info=True)

            # --- 3. 判断是否为最后交易日，并独立发送总结报告 ---
            next_day = current_date + timedelta(days=1)
            
            if next_day.weekday() == 5: # 如果明天是周六 (weekday 5)，说明今天是周五
                weekly_pl = details['endingValue'] - self.state['weekly_start_nav'] - self.state['weekly_deposits']
                self._send_summary_notification("week", weekly_pl, current_date)

            if next_day.month != current_date.month: # 如果明天是新的月份
                monthly_pl = details['endingValue'] - self.state['monthly_start_nav'] - self.state['monthly_deposits']
                self._send_summary_notification("month", monthly_pl, current_date)

            # --- 4. 最后，更新状态并保存 ---
            self.state['last_report_details'] = details
            self.save_state()

            return {'status': 'notification_sent', 'data_date': details['reportDate']}
        
        return {'status': 'no_notification_needed', 'data_date': details['reportDate']}

    def run(self):
        """运行主循环"""
        logging.info(f"启动 IBKR 资产追踪器。当前本地时间: {datetime.now()}, 当前ET时间: {self.get_current_et_time().strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        
        if self.initial_run_for_notification:
            logging.info("脚本进程启动，立即尝试执行一次数据获取和首次通知...")
            initial_report_result = self.send_daily_report()
            logging.info(f"脚本进程启动时的首次尝试完成。状态: {initial_report_result.get('status', '未知')}")

        while True:
            try:
                current_et_time = self.get_current_et_time()
                is_workday = 0 <= current_et_time.weekday() <= 4 
                is_time_to_start_hunt = (is_workday and (current_et_time.hour > 15 or (current_et_time.hour == 15 and current_et_time.minute >= 59)))

                if not self.hunt_active_for_current_cycle and is_time_to_start_hunt:
                    logging.info(f"工作日到达15:59 ET或之后。开始新的数据获取/推送轮询周期。")
                    self.hunt_active_for_current_cycle = True
                
                if self.hunt_active_for_current_cycle:
                    report_result = self.send_daily_report()
                    if report_result['status'] == 'notification_sent':
                        self.hunt_active_for_current_cycle = False 
                        fetched_date_info = report_result.get('data_date', '未知')
                        logging.info(f"成功发送通知 (报告实际日期: {fetched_date_info})。当前轮询周期结束。计算等待下一计划启动点。")
                        
                        sleep_duration = self._calculate_sleep_to_next_cycle(self.get_current_et_time())
                        next_wakeup_time = datetime.now() + timedelta(seconds=sleep_duration)
                        logging.info(f"将休眠约 {sleep_duration/3600:.2f} 小时，直到 {next_wakeup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                        time.sleep(sleep_duration)
                    else: 
                        logging.warning(f"数据获取尝试未发送通知 (状态: {report_result['status']})。将在10分钟后重试。")
                        time.sleep(600)
                else:
                    sleep_duration = self._calculate_sleep_to_next_cycle(self.get_current_et_time())
                    next_wakeup_time = datetime.now() + timedelta(seconds=sleep_duration)
                    logging.info(f"轮询未激活。将休眠约 {sleep_duration/3600:.2f} 小时，直到 {next_wakeup_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    time.sleep(max(60, sleep_duration))
            except Exception as e:
                logging.error(f"主循环发生意外错误: {str(e)}", exc_info=True)
                logging.info("发生错误，休眠10分钟后重试主逻辑。")
                time.sleep(600)

    def _calculate_sleep_to_next_cycle(self, current_et_time):
        """计算到下一个有效工作日15:59 ET的休眠秒数。"""
        now_et = current_et_time
        next_run_candidate = now_et.replace(hour=15, minute=59, second=0, microsecond=0)
        if now_et >= next_run_candidate:
            next_run_candidate = next_run_candidate + timedelta(days=1)
        while next_run_candidate.weekday() >= 5: # 5 for Saturday, 6 for Sunday
            next_run_candidate = next_run_candidate + timedelta(days=1)
        sleep_seconds = (next_run_candidate - now_et).total_seconds()
        if sleep_seconds <= 0:
            return 60.0
        return sleep_seconds

if __name__ == "__main__":
    logging.info("IBKRTracker 脚本准备启动...") 
    tracker = IBKRTracker()
    tracker.run()