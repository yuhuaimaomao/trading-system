#!/bin/bash
# 股票量化系统 - Telegram 消息监听启动脚本
# 执行时间：每个交易日 09:00

set -euo pipefail

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

# 防止重复启动
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    log "监听已在运行 (PID $(cat "$PID_FILE"))，跳过"
    exit 0
fi
rm -f "$PID_FILE"

log "=========================================="
log "Telegram 消息监听"
log "=========================================="
log "启动时间：$(date '+%Y-%m-%d %H:%M:%S')"
log "交易日：$TRADE_DATE"
log ""

cd "$PROJECT_DIR"

if [ -d "venv" ]; then
    source venv/bin/activate
fi

if [ -f .env ]; then
    set -a
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] && export "$line"
    done < .env
    set +a
fi

log "启动监听..."
python main.py listen >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
log "监听已启动 (PID $(cat "$PID_FILE"))"

wait $!
log "监听进程退出"
rm -f "$PID_FILE"
