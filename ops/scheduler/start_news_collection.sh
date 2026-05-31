#!/bin/bash
# 股票量化系统 - 财联社电报采集脚本
# 执行时间：交易日 08:00-20:00，每 20 分钟一次
# cron: */20 8-20 * * 1-5

set -euo pipefail

# cron 环境文件描述符限制
ulimit -n 4096

# 确保 homebrew 命令可用
export PATH="/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

LOG_FILE="$PROJECT_DIR/storage/logs/$(date +%Y-%m-%d)/tasks/cron_telegraph.log"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 确保日志目录存在
mkdir -p "$(dirname "$LOG_FILE")"

# 进入项目目录
cd "$PROJECT_DIR"

# 激活虚拟环境
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# 加载环境变量
if [ -f .env ]; then
    set -a
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] && export "$line"
    done < .env
    set +a
fi

log "电报采集开始"
python main.py collect --module news
log "电报采集完成"
