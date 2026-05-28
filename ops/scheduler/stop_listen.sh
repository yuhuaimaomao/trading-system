#!/bin/bash
# 股票量化系统 - Telegram 消息监听停止脚本
# 执行时间：每个交易日 18:00

set -e

export PATH="/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

TRADE_DATE=$(date +%Y-%m-%d)
LOG_FILE="$PROJECT_DIR/storage/logs/$TRADE_DATE/tasks/cron_listen.log"
PID_FILE="$PROJECT_DIR/storage/listen.pid"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

mkdir -p "$(dirname "$LOG_FILE")"

if [ ! -f "$PID_FILE" ]; then
    log "无 PID 文件，监听可能未运行"
    exit 0
fi

LISTEN_PID=$(cat "$PID_FILE")

if ! kill -0 "$LISTEN_PID" 2>/dev/null; then
    log "进程 $LISTEN_PID 已不存在"
    rm -f "$PID_FILE"
    exit 0
fi

log "停止监听进程 $LISTEN_PID..."
kill "$LISTEN_PID"

# 等待退出
for i in 1 2 3 4 5; do
    if ! kill -0 "$LISTEN_PID" 2>/dev/null; then
        log "已退出"
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 1
done

log "强制终止..."
kill -9 "$LISTEN_PID" 2>/dev/null || true
rm -f "$PID_FILE"
log "已强制终止"
