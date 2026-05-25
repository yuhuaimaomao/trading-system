#!/bin/bash
# 股票量化系统 - 周清理启动脚本
# 执行时间：每周五 18:00

set -e

# 确保 openclaw 命令可用（修复 cron 环境问题）
export PATH="/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

LOG_FILE="$PROJECT_DIR/storage/logs/$(date +%Y-%m-%d)/tasks/cron_cleanup.log"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 确保日志目录存在
mkdir -p "$(dirname "$LOG_FILE")"

log "=========================================="
log "周清理"
log "=========================================="
log "启动时间：$(date '+%Y-%m-%d %H:%M:%S')"
log "项目目录：$PROJECT_DIR"
log ""

# 进入项目目录
cd "$PROJECT_DIR"

# 激活虚拟环境
if [ -d "venv" ]; then
    log "激活虚拟环境..."
    source venv/bin/activate
fi

# 加载环境变量
if [ -f .env ]; then
    log "加载环境变量..."
    set -a
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] && export "$line"
    done < .env
    set +a
fi

# 执行清理任务
log "执行清理..."
python ops/scripts/cleanup.py 2>&1 | tee -a "$LOG_FILE"

log ""
log "=========================================="
log "清理执行完成"
log "完成时间：$(date '+%Y-%m-%d %H:%M:%S')"
log "=========================================="
