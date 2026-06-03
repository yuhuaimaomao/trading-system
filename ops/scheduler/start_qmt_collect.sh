#!/bin/bash
# QMT Collector — 独立数据采集进程（TCP server + DB 容灾）
# 执行时间：每个交易日 09:24-15:00（自管理生命周期）

set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

LOG_FILE="$PROJECT_DIR/storage/logs/$(date +%Y-%m-%d)/tasks/cron_qmt_collect.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

mkdir -p "$(dirname "$LOG_FILE")"

log "=========================================="
log "QMT Collector 启动"
log "启动时间：$(date '+%Y-%m-%d %H:%M:%S')"
log "项目目录：$PROJECT_DIR"
log ""

cd "$PROJECT_DIR"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

if [ -f .env ]; then
    log "加载环境变量..."
    set -a
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] && export "$line"
    done < .env
    set +a
fi

log "启动 QMT Collector..."
python main.py qmt-collect 2>&1 | tee -a "$LOG_FILE"

log ""
log "=========================================="
log "QMT Collector 退出"
log "完成时间：$(date '+%Y-%m-%d %H:%M:%S')"
log "=========================================="
