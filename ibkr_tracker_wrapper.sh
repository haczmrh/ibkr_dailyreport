#!/bin/bash

# 设置工作目录
cd "$(dirname "$0")"

# 检查进程是否在运行
check_process() {
    pgrep -f "python3 ibkr_net_value_tracker.py" > /dev/null
    return $?
}

# 启动主程序
start_process() {
    nohup python3 ibkr_net_value_tracker.py > ibkr_tracker.log 2>&1 &
}

# 主循环
while true; do
    if ! check_process; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - 进程未运行，正在重启..." >> ibkr_tracker_wrapper.log
        start_process
    fi
    sleep 60
done 