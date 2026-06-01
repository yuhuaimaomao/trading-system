#!/bin/bash
# 启动交易系统：采集器 + 盯盘
# 用法: bash scripts/start_trading.sh [monitor|collect|both]
#
# 采集器独立运行到收盘，午休自动暂停，Watcher 重启不影响采集器

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PID_DIR="$ROOT/storage"
COLLECTOR_PID_FILE="$PID_DIR/collector.pid"

start_collector() {
    if [ -f "$COLLECTOR_PID_FILE" ]; then
        pid=$(cat "$COLLECTOR_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "采集器已在运行 (PID $pid)"
            return 0
        fi
        rm -f "$COLLECTOR_PID_FILE"
    fi

    echo "启动 QMT 全市场采集器..."
    PYTHONPATH=. .venv/bin/python main.py qmt-collect &
    echo $! > "$COLLECTOR_PID_FILE"
    echo "采集器 PID: $(cat $COLLECTOR_PID_FILE)"
}

start_monitor() {
    echo "启动盯盘..."
    PYTHONPATH=. .venv/bin/python main.py monitor
}

stop_monitor_only() {
    local watcher_pid=$(cat "$PID_DIR/watcher.pid" 2>/dev/null)
    if [ -n "$watcher_pid" ] && kill -0 "$watcher_pid" 2>/dev/null; then
        kill "$watcher_pid"
        echo "盯盘已停止 (PID $watcher_pid)"
    fi
}

case "${1:-both}" in
    collect)
        start_collector
        ;;
    monitor)
        start_collector  # 盯盘依赖采集器
        start_monitor
        ;;
    both)
        start_collector
        start_monitor
        ;;
    stop)
        stop_monitor_only
        ;;
    *)
        echo "用法: $0 [collect|monitor|both|stop]"
        exit 1
        ;;
esac
