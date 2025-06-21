import requests
import time
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import logging
import logging.handlers # 重要：导入日志处理器模块
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
import pytz
from telegram_notifier import send_telegram_notification

# 添加当前目录到 Python 路径，确保 push 模块能被找到
# 使用 resolve().parent 获取脚本真实所在目录，更健壮
sys.path.append(str(Path(__file__).resolve().parent))
from push import send_bark_notification

# 加载 .env 文件中的环境变量
load_dotenv()

# --- 配置日志记录 (使用 TimedRotatingFileHandler 实现每日轮换) ---
log_dir = '/opt/ibkr_net_worth_tracker/logs'
os.makedirs(log_dir, exist_ok=True) # 确保日志目录存在
log_file_basename = 'ibkr_tracker.log' # 日志文件的基础名
log_file_path = os.path.join(log_dir, log_file_basename)

# 获取根日志记录器 (root logger)
logger = logging.getLogger()
logger.setLevel(logging.INFO) # 设置全局日志级别 (生产环境推荐INFO, 调试时可改为DEBUG)

# 创建一个格式化器，用于定义日志的输出格式
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# 创建 TimedRotatingFileHandler
# when='midnight': 在午夜进行轮换
# interval=1: 每天轮换一次
# backupCount=30: 保留最近30个旧的日志文件
timed_file_handler = logging.handlers.TimedRotatingFileHandler(
    log_file_path,
    when='midnight',
    interval=1,
    backupCount=30,
    encoding='utf-8' # 使用UTF-8编码
)
timed_file_handler.setFormatter(formatter)
timed_file_handler.suffix = "%Y-%m-%d" # 设置轮换后的文件名后缀格式 (例如 .log.2025-05-08)

# 创建 StreamHandler 用于在控制台输出日志 (方便直接查看)
stream_handler = logging.StreamHandler(sys.stdout) # 输出到标准输出
stream_handler.setFormatter(formatter)

# 清理根记录器中可能已存在的处理器，以避免重复日志
if logger.hasHandlers():
    logger.handlers.clear()

# 将新的处理器添加到根记录器
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
        self.hunt_active_for_current_cycle = False # 标记一个15:59启动的获取/推送周期是否正在进行中

    def get_current_et_time(self):
        """获取当前美东时间"""
        return datetime.now(self.et_timezone)
        
    def get_account_summary(self):
        """获取账户摘要信息。"""
        try:
            params_send = {'t': self.flex_token, 'q': self.query_id, 'v': '3'}
            logging.debug("步骤1/5: 正在发送请求以生成报告...") # 日志级别调整为 DEBUG
            response_send = requests.get(self.send_request_url, params=params_send, timeout=30)
            logging.debug(f"步骤1/5: 发送请求响应状态码: {response_send.status_code}") # 日志级别调整为 DEBUG
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
            logging.debug(f"步骤2/5: 获取到 Reference Code: {reference_code}") # 日志级别调整为 DEBUG
            
            logging.debug("步骤3/5: 等待30秒以便IB系统生成报告...") # 日志级别调整为 DEBUG
            time.sleep(30) 

            params_get = {'t': self.flex_token, 'q': reference_code, 'v': '3'}
            logging.debug(f"步骤4/5: 正在使用 Reference Code {reference_code} 获取报告数据...") # 日志级别调整为 DEBUG
            response_get = requests.get(self.get_statement_url, params=params_get, timeout=30)
            logging.debug(f"步骤4/5: 获取报告响应状态码: {response_get.status_code}") # 日志级别调整为 DEBUG
            response_get.raise_for_status()
            
            xml_content = response_get.content
            try:
                root_get = ET.fromstring(xml_content) 
            except ET.ParseError as e_parse:
                try:
                    problem_xml_text = xml_content.decode('utf-8', errors='replace')[:500]
                except Exception:
                    problem_xml_text = str(xml_content[:500])
                logging.error(f"XML 解析错误: {str(e_parse)}. 问题XML文本 (前500字符/字节): {problem_xml_text}")
                return None

            error_code = root_get.findtext('ErrorCode')
            if error_code:
                error_message_get = root_get.findtext('ErrorMessage', "报告获取返回错误码，但无ErrorMessage")
                logging.error(f"获取报告时遇到问题: ErrorCode {error_code} - {error_message_get}")
                if error_code in ['1018', '1019', '1020']: 
                     logging.info("报告尚未准备好 (错误码 %s)，将由主循环重试。", error_code) # 这条INFO保留
                return None

            flex_statements = root_get.find('.//FlexStatements')
            if flex_statements is None or not list(flex_statements): 
                logging.error("响应XML中未找到 FlexStatements 节点或该节点为空。")
                return None
            statement = flex_statements.find('FlexStatement')
            if statement is None:
                logging.error("FlexStatements 中未找到 FlexStatement 节点。")
                return None
                
            raw_from_date_str = statement.get('fromDate') 
            report_date_display = ""
            current_et_for_fallback = self.get_current_et_time().strftime("%Y-%m-%d")
            if raw_from_date_str:
                try:
                    report_date_display = f"{raw_from_date_str[:4]}-{raw_from_date_str[4:6]}-{raw_from_date_str[6:]}"
                except IndexError:
                    logging.warning(f"fromDate格式错误: {raw_from_date_str}，将使用当前ET日期 ({current_et_for_fallback}) 作为报告显示日期。")
                    report_date_display = current_et_for_fallback
                    raw_from_date_str = None 
            else:
                logging.warning(f"报告中未提供 fromDate，将使用当前ET日期 ({current_et_for_fallback}) 作为报告显示日期。")
                report_date_display = current_et_for_fallback

            change_in_nav = statement.find('ChangeInNAV')
            if change_in_nav is None:
                logging.error("未找到 ChangeInNAV 数据。")
                return None 
                
            account_details = {
                'startingValue': float(change_in_nav.get('startingValue', 0)),
                'mtm': float(change_in_nav.get('mtm', 0)),
                'endingValue': float(change_in_nav.get('endingValue', 0)),
                'reportDate': report_date_display, 
                'depositsWithdrawals': float(change_in_nav.get('depositsWithdrawals', 0)),
                'raw_from_date': raw_from_date_str 
            }
            # 这条成功获取的日志非常重要，保留为 INFO
            logging.info(f"成功获取并解析账户数据，报告日期: {report_date_display} (原始fromDate: {raw_from_date_str if raw_from_date_str else 'N/A'})")
            return account_details
            
        except requests.exceptions.Timeout:
            logging.error("请求IBKR API超时。")
            return None
        except requests.exceptions.HTTPError as e:
            error_text = e.response.text[:200] if hasattr(e.response, 'text') else "无响应体" 
            status_code_info = e.response.status_code if hasattr(e.response, 'status_code') else "无状态码"
            logging.error(f"HTTP 错误: {status_code_info} - {error_text}")
            return None
        except Exception as e: # 其他所有requests相关错误或其他类型错误
            logging.error(f"获取账户信息时发生未知错误: {str(e)}", exc_info=True)
            return None
            
    def send_daily_report(self):
        """尝试获取账户数据，并根据数据的新鲜程度决定是否发送通知。"""
        logging.debug("调用 get_account_summary 获取数据...") # 日志级别调整为 DEBUG
        details = self.get_account_summary()
        
        if details is None:
            logging.warning("get_account_summary 未能成功获取或解析数据。") # 这条WARNING保留
            return {'status': 'error_fetching'}
            
        current_raw_fromdate = details.get('raw_from_date')
        report_display_date = details.get('reportDate', '未知日期')

        notify = False
        log_message_prefix = ""
        
        if self.initial_run_for_notification:
            if current_raw_fromdate:
                notify = True
                log_message_prefix = "脚本进程首次运行推送日报"
            else:
                logging.info(f"脚本进程首次运行尝试获取数据，但报告 ({report_display_date}) 中缺少有效原始fromDate，本次不发送通知，将等待下一次有效数据。")
        elif current_raw_fromdate is not None and current_raw_fromdate != self.last_notified_raw_fromdate:
            notify = True
            log_message_prefix = "检测到新数据推送日报"
        elif current_raw_fromdate is None:
            logging.info(f"报告 ({report_display_date}) 中无有效原始fromDate，无法判断是否为新数据，默认不发送通知。")
        else:
            logging.info(f"报告数据 (报告日期: {report_display_date}, fromDate: {current_raw_fromdate}) 与上次已通知数据相同，跳过本次通知。")

        if notify:
            self.last_notified_raw_fromdate = current_raw_fromdate
            if self.initial_run_for_notification:
                 self.initial_run_for_notification = False

            net_change = details['endingValue'] - details['startingValue'] - details['depositsWithdrawals']
            change_text = "😴【一般】"
            if net_change > 1000:  change_text = "🥳【上头了】"
            elif net_change < -1000: change_text = "😩【狂泻】"
            
            change_display = "涨" if net_change >= 0 else "跌"
            
            message = f"{change_display}: ${net_change:,.2f}\n净资产: ${details['endingValue']:,.2f}"
            if details['depositsWithdrawals'] > 0:
                message += f"\n入金: ${details['depositsWithdrawals']:,.2f}"
            elif details['depositsWithdrawals'] < 0:
                message += f"\n出金: ${abs(details['depositsWithdrawals']):,.2f}"
            
            title = f"{change_text} IB {details['reportDate']} 日报"
            logging.info(f"{log_message_prefix}: {title} | 内容: {message}") # 推送内容日志保留INFO
            try:
                send_bark_notification(title, message)
                logging.info("Bark 通知已发送。") # 保留INFO
            except Exception as e:
                logging.error(f"发送 Bark 通知失败: {str(e)}", exc_info=True)
            # 发送 Telegram 通知 <--- 新增开始 --->
            try:
                send_telegram_notification(title, message) # 调用新的通知函数
                # 日志记录已在 telegram_notifier.py 中处理
            except Exception as e:
                # 通常 telegram_notifier 内部会处理并记录错误，这里可以捕获以防万一
                logging.error(f"尝试调用 send_telegram_notification 时发生外部错误: {str(e)}", exc_info=True)
            # <--- 新增结束 --->

            return {'status': 'notification_sent', 'data_date': report_display_date, 'raw_from_date': current_raw_fromdate}
        else:
            return {'status': 'no_notification_needed', 'data_date': report_display_date, 'raw_from_date': current_raw_fromdate}

    def _calculate_sleep_to_next_cycle(self, current_et_time):
        """计算到下一个有效工作日15:59 ET的休眠秒数。"""
        now_et = current_et_time
        next_run_candidate = now_et.replace(hour=15, minute=59, second=0, microsecond=0)

        if now_et >= next_run_candidate:
            next_run_candidate = next_run_candidate + timedelta(days=1)
            next_run_candidate = next_run_candidate.replace(hour=15, minute=59, second=0, microsecond=0)

        while next_run_candidate.weekday() >= 5: # 5 for Saturday, 6 for Sunday
            next_run_candidate = next_run_candidate + timedelta(days=1)
            next_run_candidate = next_run_candidate.replace(hour=15, minute=59, second=0, microsecond=0)
            
        sleep_seconds = (next_run_candidate - now_et).total_seconds()

        if sleep_seconds <= 0:
            logging.debug(f"_calculate_sleep_to_next_cycle 计算出非正数休眠时间 ({sleep_seconds}s)。将默认休眠60秒。")
            return 60.0
        
        return sleep_seconds

    def run(self):
        """
        运行主循环:
        - 脚本进程启动时立即尝试一次（作为首次通知）。
        - 工作日15:59 ET启动一个新的获取/推送周期（如果当前没有激活的周期）。
        - 一旦获取/推送周期被激活，它会每10分钟尝试获取数据，直到数据有变化并成功触发推送通知。
          这个激活的周期会持续运行，即便是跨天或进入周末，直到成功推送。
        - 成功推送后，此周期结束，等待下一个有效工作日的15:59 ET启动新周期。
        """
        logging.info(f"启动 IBKR 资产追踪器。当前本地时间: {datetime.now()}, 当前ET时间: {self.get_current_et_time().strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        
        if self.initial_run_for_notification:
            logging.info("脚本进程启动，立即尝试执行一次数据获取和首次通知...")
            initial_report_result = self.send_daily_report()
            logging.info(f"脚本进程启动时的首次尝试完成。状态: {initial_report_result.get('status', '未知')}")

        try:
            # 初始化 last_processed_day_str 已被移除，因为不再需要每日重置 hunt_active_for_current_cycle
            pass # 保留try-except结构，以防未来需要在启动时做其他可能失败的初始化
        except Exception as e:
            logging.error(f"run方法初始化阶段发生错误: {e}", exc_info=True)
            # 关键初始化失败，可能需要决定是否继续或退出
            # 为简单起见，这里继续，但实际生产中可能需要更复杂的处理


        while True:
            try:
                current_et_time = self.get_current_et_time()
                current_day_et_str = current_et_time.strftime("%Y-%m-%d") 
                is_workday = 0 <= current_et_time.weekday() <= 4 
                
                is_time_to_start_new_scheduled_hunt = (is_workday and 
                                                       (current_et_time.hour > 15 or 
                                                        (current_et_time.hour == 15 and current_et_time.minute >= 59)))

                if not self.hunt_active_for_current_cycle and is_time_to_start_new_scheduled_hunt:
                    logging.info(f"工作日 {current_day_et_str} 到达15:59 ET或之后。开始新的数据获取/推送轮询周期。")
                    self.hunt_active_for_current_cycle = True
                
                if self.hunt_active_for_current_cycle:
                    logging.debug(f"获取/推送轮询周期激活中。尝试获取数据 (当前ET: {current_day_et_str} {current_et_time.strftime('%H:%M:%S')})...") # 日志级别调整为 DEBUG
                    report_result = self.send_daily_report()

                    if report_result['status'] == 'notification_sent':
                        self.hunt_active_for_current_cycle = False 
                        fetched_date_info = report_result.get('data_date', '未知')
                        logging.info(f"成功发送通知 (报告实际日期: {fetched_date_info})。当前轮询周期结束。计算等待下一计划启动点。")
                        
                        sleep_duration_seconds = self._calculate_sleep_to_next_cycle(current_et_time)
                        next_wakeup_time = current_et_time + timedelta(seconds=sleep_duration_seconds)
                        logging.info(f"将休眠约 {sleep_duration_seconds/3600:.2f} 小时，直到 {next_wakeup_time.strftime('%Y-%m-%d %H:%M:%S %Z%z')} (预计下一个工作日15:59 ET)。")
                        time.sleep(sleep_duration_seconds)
                        continue 
                    
                    elif report_result['status'] == 'no_notification_needed':
                        raw_date = report_result.get('raw_from_date', 'N/A')
                        # 这条INFO日志告知了获取数据后的判断结果，很重要，保留
                        logging.info(f"获取到数据 (报告实际日期: {report_result.get('data_date', '未知')}, fromDate: {raw_date})，但无需发送新通知。将在10分钟后重试。")
                        time.sleep(600) 
                        continue
                    
                    else: # 'error_fetching'
                        # 这条WARNING日志很重要，保持
                        logging.warning(f"数据获取尝试失败或报告未就绪 (状态: {report_result['status']})。将在10分钟后重试。")
                        time.sleep(600) 
                        continue
                
                else: # 当前没有激活的获取/推送轮询周期
                    if is_workday and not is_time_to_start_new_scheduled_hunt: 
                        target_start_time = current_et_time.replace(hour=15, minute=59, second=0, microsecond=0)
                        if current_et_time < target_start_time: 
                            sleep_duration = (target_start_time - current_et_time).total_seconds()
                            sleep_duration = max(10, sleep_duration) 
                            logging.debug(f"工作日 ({current_day_et_str})，轮询未激活。将休眠约 {sleep_duration/60:.1f} 分钟，等待至当日15:59 ET ({target_start_time.strftime('%Y-%m-%d %H:%M:%S %Z%z')})。")
                            time.sleep(sleep_duration)
                            continue
                    
                    logging.debug(f"轮询未激活 ({current_day_et_str} {current_et_time.strftime('%H:%M:%S')})。计算等待下一工作日15:59 ET计划启动点。")
                    sleep_duration_seconds = self._calculate_sleep_to_next_cycle(current_et_time)
                    next_wakeup_time = current_et_time + timedelta(seconds=sleep_duration_seconds)
                    logging.info(f"将休眠约 {sleep_duration_seconds/3600:.2f} 小时，直到 {next_wakeup_time.strftime('%Y-%m-%d %H:%M:%S %Z%z')}。")
                    time.sleep(sleep_duration_seconds)
                    continue

            except Exception as e:
                logging.error(f"主循环发生意外错误: {str(e)}", exc_info=True)
                logging.info("发生错误，休眠10分钟后重试主逻辑。")
                time.sleep(600)

if __name__ == "__main__":
    logging.info("IBKRTracker 脚本准备启动...") 
    tracker = IBKRTracker()
    tracker.run()