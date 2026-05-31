# -*- coding: utf-8 -*-
"""
复盘预测验证脚本

用法: python scripts/verify_predictions.py [YYYY-MM-DD]
对比指定日期的复盘预测 vs 下一交易日实际表现。
默认检查昨天的预测（今天有数据可对比）。
"""
import sqlite3, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from system.config.settings import DATABASE_PATH
from system.config.trading_calendar import get_next_trading_day


def verify(push_date: str):
    check_date = get_next_trading_day(push_date)
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row

    predictions = [dict(r) for r in conn.execute(
        "SELECT * FROM review_predictions WHERE push_date=? AND is_correct IS NULL", (push_date,)
    ).fetchall()]

    if not predictions:
        print(f"{push_date} 无待验证预测")
        conn.close()
        return

    updated = 0
    for p in predictions:
        if p['pred_type'] == 'index':
            result = _verify_index(conn, p, check_date)
        elif p['pred_type'] == 'sector':
            result = _verify_sector(conn, p, check_date)
        elif p['pred_type'] == 'scenario':
            result = _verify_scenario(conn, p, check_date)
        else:
            continue

        if result is not None:
            conn.execute(
                "UPDATE review_predictions SET actual_result=?, is_correct=?, checked_date=? WHERE id=?",
                (result['actual'], 1 if result['correct'] else 0, check_date, p['id'])
            )
            status = '✅' if result['correct'] else '❌'
            print(f"  {status} [{p['pred_type']}] {p['target_name']}: 预测={p['pred_direction']} 实际={result['actual']}")
            updated += 1

    conn.commit()
    conn.close()
    print(f"\n验证完成: {updated}/{len(predictions)} 条, 日期 {push_date} → {check_date}")


def _verify_index(conn, p, check_date):
    """对比指数预测：方向是否一致"""
    row = conn.execute(
        "SELECT change_percent FROM index_realtime_data WHERE index_code=? AND trade_date=?",
        (_index_code(p['target_name']), check_date)
    ).fetchone()
    if not row:
        return None
    chg = row['change_percent'] or 0
    direction = p['pred_direction']
    actual_dir = '单边上涨' if chg > 1.5 else ('震荡偏多' if chg > 0 else ('震荡偏空' if chg > -1.5 else '单边下跌'))
    correct = _direction_match(direction, actual_dir)
    return {'actual': f"{actual_dir}({chg:+.2f}%)", 'correct': correct}


def _verify_sector(conn, p, check_date):
    """对比板块预测：走势是否符合"""
    row = conn.execute(
        "SELECT change_pct FROM sector_info WHERE sector_name=? AND trade_date=?", (p['target_name'], check_date)
    ).fetchone()
    if not row:
        # fallback to sector_industry/concept
        for tbl in ['sector_industry', 'sector_concept']:
            row = conn.execute(f"SELECT change_pct FROM {tbl} WHERE name=? AND trade_date=?", (p['target_name'], check_date)).fetchone()
            if row: break
    if not row:
        return None
    chg = row[0] or 0
    pred = p['pred_direction']
    # 简化验证逻辑
    if pred == '一日游风险':
        correct = chg <= 0.5  # 一日游=涨不动或跌
    elif pred == '主线延续':
        correct = chg > 0  # 延续=继续涨
    elif pred == '分歧后回流':
        correct = chg > -0.5  # 分歧后回流=小跌后涨，最终不跌太多
    elif pred == '退潮':
        correct = chg < 0  # 退潮=跌
    elif pred == '新方向发酵':
        correct = chg > 0.5  # 新方向=有涨幅
    else:
        correct = None
    if correct is None:
        return None
    return {'actual': f"涨跌{chg:+.2f}%", 'correct': correct}


def _verify_scenario(conn, p, check_date):
    """情景验证：简化处理，后续可扩展"""
    return None  # 情景准确度需要综合判断，先标记为手动验证


def _index_code(name):
    return {'上证指数': 'sh000001', '创业板指': 'sz399006', '深证成指': 'sz399001', '沪深300': 'sh000300'}.get(name, '')


def _direction_match(pred, actual):
    """判断两个方向描述是否一致"""
    bullish = {'单边上涨', '震荡偏多'}
    bearish = {'单边下跌', '震荡偏空'}
    if (pred in bullish and actual in bullish) or (pred in bearish and actual in bearish):
        return True
    if pred == '窄幅震荡' and actual not in bullish and actual not in bearish:
        return True
    return False


if __name__ == '__main__':
    if len(sys.argv) > 1:
        date = sys.argv[1]
    else:
        from system.config.trading_calendar import get_previous_trading_day
        date = get_previous_trading_day(datetime.now().strftime('%Y-%m-%d'))
        print(f"默认检查上一个复盘日: {date}")
    verify(date)
