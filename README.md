# IBKR 资产追踪器

## 文件说明
- `ibkr_net_value_tracker.py` - 主程序文件
- `push.py` - 推送通知模块
- `requirements.txt` - 依赖文件
- `ibkr_tracker_wrapper.sh` - 包装脚本
- `ibkr_tracker.service` - systemd 服务配置
- `logs/` - 日志目录

## 全新服务器部署教程

### 1. 获取项目代码
```bash
# 克隆项目到本地
git clone https://github.com/haczmrh/ibkr_dailyreport.git

# 进入项目目录
cd ibkr_dailyreport
```

### 2. 安装依赖
```bash
# 安装 Python3 和 pip
sudo apt update
sudo apt install python3 python3-pip

# 安装项目依赖
pip3 install -r requirements.txt
```

### 3. 配置环境变量
```bash
# 创建 .env 文件
touch .env

# 编辑 .env 文件，添加以下内容
cat > .env << EOF
# IBKR API 配置
IB_TOKEN=你的IB Token
IB_QUERY_ID=你的IB Query ID

# Bark 推送配置
BARK_URL=你的Bark推送URL

# TELEGRAM 推送配置
TELEGRAM_BOT_TOKEN=你的Telegram_Bot_Token_这里
#TELEGRAM_CHAT_IDS=chat_id_1,chat_id_2,chat_id_3 注意变量名改为了复数，并用逗号分隔

### 4. 配置 systemd 服务
```bash
# 复制服务配置文件
sudo cp ibkr_tracker.service /etc/systemd/system/

# 重新加载 systemd 配置
sudo systemctl daemon-reload

# 启用并启动服务
sudo systemctl enable ibkr_tracker
sudo systemctl start ibkr_tracker

# 检查服务状态
sudo systemctl status ibkr_tracker
```

### 5. 验证部署
```bash
# 查看日志确认程序是否正常运行
tail -f logs/ibkr_tracker_$(date +%Y%m%d).log
```
--------------------------------------------------------------


## 常用运行命令

### 手动运行
```bash
# 1. 进入程序目录
cd /opt/ibkr_dailyreport

# 2. 后台运行程序
nohup python3 ibkr_net_value_tracker.py > /dev/null 2>&1 &

# 3. 检查是否在运行
ps aux | grep ibkr_net_value_tracker.py

# 4. 如果要停止程序
pkill -f ibkr_net_value_tracker.py
```

### 使用 systemd 服务
```bash
# 启动服务
sudo systemctl start ibkr_tracker

# 停止服务
sudo systemctl stop ibkr_tracker

# 重启服务
sudo systemctl restart ibkr_tracker

# 查看服务状态
sudo systemctl status ibkr_tracker

# 设置开机自启
sudo systemctl enable ibkr_tracker
```

### 查看日志
```bash
# 查看最新日志
tail -f logs/ibkr_tracker_$(date +%Y%m%d).log
```

## 注意事项

-   **程序执行逻辑**:
    -   **首次运行**: 脚本进程一旦启动，会立即尝试执行一次数据获取，并根据数据情况尝试进行首次通知。
    -   **计划执行 (美东时间 周一至周五)**:
        -   脚本会在每日的美东时间下午 15:59 (ET) 准时启动当日数据的获取流程。
        -   如果成功从IBKR获取到数据报告，则认为当日的获取任务完成，脚本将休眠并等待下一个计划执行日的到来。
        -   如果在15:59的尝试中获取数据失败（例如API暂时无响应、报告未就绪等），程序会自动进入重试模式，每隔10分钟尝试一次，直到成功获取到数据，或者当日的美东时间结束。
    -   **周末 (美东时间 周六、周日)**:
        -   在周末，脚本不会执行下午15:59的数据获取任务。
        -   程序会处于较低频率的运行状态（例如每小时检查一次时间），主要为了判断何时进入下一个工作日的执行窗口。此期间，脚本会记录表明当前是周末并跳过数据获取任务的简要状态日志。

-   **日志记录**:
    -   脚本运行过程中的所有关键操作和信息都会被记录到日志中，这包括：程序启动/退出、每次尝试获取数据、API的响应状态、解析数据的结果、错误信息、发送通知的详情以及内部的调度决策等。
    -   日志文件默认保存在服务器的 `/opt/ibkr_dailyreport/logs/ibkr_tracker.log` 文件中。
    -   新的日志信息会持续追加到此文件的末尾。

-   **时间与配置**:
    -   **时间基准**: 程序内部的所有与交易日、执行时间相关的判断（例如，是否为工作日，是否到达15:59 ET）都是基于 **美东时间 (ET/EDT)** 进行的。
    -   **服务器时间**: 请务必确保运行脚本的服务器本身的系统时间是准确的。虽然程序会转换为美东时间进行判断，但准确的本地时间是正确转换的基础。日志条目中的时间戳（如 `2025-05-07 02:08:00,123`）通常会反映服务器的本地时间。
    -   **网络访问**: 确保服务器可以稳定访问盈透证券 (IBKR) 的Flex Web Service API (域名通常是 `https://ndcdyn.interactivebrokers.com`) 以及你配置的 Bark 推送服务地址。
    -   **`.env` 文件**: 确保在脚本运行的同目录下正确放置了 `.env` 文件，并且文件内已填写了有效的 `IB_FLEX_TOKEN` (IB Flex Query Token) 和 `IB_QUERY_ID` (IB Flex Query ID)。

-   **运行环境要求**:
    -   **Python 版本**: 建议使用 Python 3.6 或更高版本。
    -   **必需的 Python 库**:
        -   `requests` (用于发送HTTP请求)
        -   `python-dotenv` (用于加载 `.env` 环境变量)
        -   `pytz` (用于处理时区，特别是美东时间)
        (以及脚本同目录下的 `push.py` 文件，用于Bark通知)