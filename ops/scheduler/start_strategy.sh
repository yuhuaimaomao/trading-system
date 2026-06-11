#!/bin/bash
# 股票量化系统 - 盘前趋势筛选启动脚本
# 执行时间：每个交易日 08:30

set -euo pipefail

# cron 环境默认 fd 限制 256，子进程隔离后主进程 fd 低但预留余量
ulimit -n 16384

# 确保 homebrew 命令可用（修复 cron 环境问题）
export PATH="/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

LOG_FILE="$PROJECT_DIR/storage/logs/$(date +%Y-%m-%d)/tasks/cron_strategy.log"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 确保日志目录存在
mkdir -p "$(dirname "$LOG_FILE")"

log "=========================================="
log "盘前趋势筛选"
log "=========================================="
log "启动时间：$(date '+%Y-%m-%d %H:%M:%S')"
log "项目目录：$PROJECT_DIR"
log ""

# 进入项目目录
cd "$PROJECT_DIR"

# 激活虚拟环境
if [ -d ".venv" ]; then
    log "激活虚拟环境..."
    source .venv/bin/activate
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

# 执行盘前趋势筛选任务
log "执行趋势筛选..."
python main.py strategy 2>&1 | tee -a "$LOG_FILE"

log ""
log "=========================================="
log "盘前趋势筛选执行完成"
log "完成时间：$(date '+%Y-%m-%d %H:%M:%S')"
log "=========================================="
