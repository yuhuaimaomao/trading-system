#!/bin/bash
# 股票量化系统 - 盘中电报采集脚本
# 执行时间：每5分钟 9:00-17:00

set -e

export PATH="/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

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

exec python main.py collect --module news >> storage/logs/cron_news_collection.log 2>&1
