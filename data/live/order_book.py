"""盘口 + 大单流向 + 合约信息 — 从 QMT 获取实时交易微观数据。

纯数据获取，不依赖 Watcher state。
"""


def get_order_book_imbalance(code: str, price: float, qmt) -> tuple[float, str]:
    """五档盘口买卖力量对比。返回 (bid_ratio, reason)。

    bid_ratio = 买盘总量 / (买盘总量 + 卖盘总量)，>0.5 买方占优。
    """
    if not qmt:
        return 0.5, ""
    try:
        detail = qmt.get_quote_detail(code)
        if not detail:
            return 0.5, ""

        ask_vols = detail.get("askVol", [])
        bid_vols = detail.get("bidVol", [])
        if not ask_vols or not bid_vols:
            return 0.5, ""

        total_bid = sum(float(v) for v in bid_vols[:5] if v)
        total_ask = sum(float(v) for v in ask_vols[:5] if v)
        total = total_bid + total_ask
        if total <= 0:
            return 0.5, ""

        ratio = total_bid / total

        if ratio >= 0.7:
            return ratio, "买盘强劲"
        elif ratio >= 0.55:
            return ratio, "买盘略强"
        elif ratio <= 0.3:
            return ratio, "卖盘沉重"
        elif ratio <= 0.45:
            return ratio, "卖盘略强"
        return ratio, "买卖均衡"
    except Exception:
        return 0.5, ""


def get_big_order_direction(code: str, qmt) -> tuple[float, str]:
    """逐笔成交大单流向分析。返回 (buy_ratio, reason)。

    统计近200笔成交中大单(>=5万元)的买卖方向，>0.55 主力买入。
    """
    if not qmt:
        return 0.5, ""
    try:
        ticks = qmt.get_ticks(code)
        if not ticks or len(ticks) < 20:
            return 0.5, ""

        big_buy_amount = 0.0
        big_sell_amount = 0.0

        prev_amount = None
        for t in ticks:
            amt = float(t.get("amount", 0))
            direction = t.get("direction", "")
            if prev_amount is not None:
                trade_amt = amt - prev_amount
                if trade_amt > 50000:
                    if direction == "buy":
                        big_buy_amount += trade_amt
                    elif direction == "sell":
                        big_sell_amount += trade_amt
            prev_amount = amt

        total_big = big_buy_amount + big_sell_amount
        if total_big <= 0:
            return 0.5, ""

        ratio = big_buy_amount / total_big

        if ratio >= 0.65:
            return ratio, f"大单买入主导({ratio:.0%})"
        elif ratio >= 0.55:
            return ratio, f"大单偏买({ratio:.0%})"
        elif ratio <= 0.35:
            return ratio, f"大单卖出主导({1 - ratio:.0%})"
        elif ratio <= 0.45:
            return ratio, f"大单偏卖({1 - ratio:.0%})"
        return ratio, "大单均衡"
    except Exception:
        return 0.5, ""


def get_instrument_info(code: str, qmt, cache: dict) -> dict:
    """获取合约基本信息（涨跌停价、股本等），缓存整日。

    Args:
        code: 股票代码
        qmt: QMT QuoteClient
        cache: instrument_cache dict (持久化，不在 scan_count 刷新)
    """
    if code in cache:
        return cache[code]
    info = {}
    if qmt:
        try:
            data = qmt.get_instrument(code)
            if data:
                info = {
                    "float_share": float(data.get("floatShare", 0)),
                    "total_share": float(data.get("totalShare", 0)),
                    "up_stop": float(data.get("upStopPrice", 0)),
                    "down_stop": float(data.get("downStopPrice", 0)),
                    "pre_close": float(data.get("preClose", 0)),
                }
        except Exception:
            pass
    cache[code] = info
    return info
