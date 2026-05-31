"""
复盘数据格式化器

职责：把 review_analyzer 查出的原始数据转成 prompt 里的文本块。
不查 DB、不调 AI，纯数据 → 文本转换。
"""


def _sector_tag(name: str, code_map: dict = None) -> str:
    """板块名 + 编码，如 '绿色电力(BK1024)'。code_map 为 None 时只返回名称。"""
    if not code_map or not name:
        return name or ""
    code = code_map.get(name, "")
    return f"{name}({code})" if code else name


def format_chain_ladder(
    chain_data: dict, promotion_rates: list = None, sector_code_map: dict = None
) -> str:
    """格式化连板梯队（含完整行情数据 + 板块 + 封单比 + 晋级率）"""
    if not chain_data:
        return "今日无连板股（≥2板）"
    lines = []
    for board in sorted(chain_data.keys(), reverse=True):
        stocks = chain_data[board]
        lines.append(f"  {board}板（{len(stocks)}只）：")
        for s in stocks:
            seal = f" 首次封板{s['first_seal']}" if s.get("first_seal") else ""
            parts = []
            if s.get("industry"):
                parts.append(_sector_tag(s["industry"], sector_code_map))
            parts.extend(
                [_sector_tag(c, sector_code_map) for c in s.get("concepts", [])]
            )
            ind = f" [{', '.join(parts)}]" if parts else ""
            # 封单金额 + 封单比
            seal_amt = s.get("seal_amount", 0) or 0
            turnover_wan = s.get("turnover_wan", 0) or 0
            seal_amt_yi = seal_amt / 100000000
            seal_ratio = (
                (seal_amt / 10000 / turnover_wan * 100) if turnover_wan > 0 else 0
            )
            seal_str = f" 封板资金{seal_amt_yi:.1f}亿" if seal_amt_yi > 0 else ""
            seal_ratio_str = f" 封单比{seal_ratio:.1f}%" if seal_ratio > 0 else ""
            lines.append(
                f"    {s['name']}({s['code']}){ind} 涨{s['change']:+.2f}% "
                f"换手{s['turnover']:.1f}% 量比{s['vol_ratio']:.1f} 振幅{s['amplitude']:.1f}% "
                f"市值{s['mcap']:.0f}亿 主力净{s['mf_wan']:+.0f}万({s['mf_ratio']:.1f}%) "
                f"超大单{s['sl_wan']:+.0f}万 "
                f"价{s.get('price', 0):.2f} MA5={s.get('ma5', 0):.2f} MA10={s.get('ma10', 0):.2f} MA20={s.get('ma20', 0):.2f} "
                f"MA5斜率{s.get('ma5_angle', 0):.1f}%"
                f"{seal}{seal_str}{seal_ratio_str}"
            )

    # 连板晋级率（近 3 日）
    if promotion_rates:
        lines.append("")
        lines.append("  【连板晋级率】")
        for period in promotion_rates:
            pr = period["rates"]
            if not pr:
                continue
            lines.append(f"    {period['label']}：")
            for board in sorted(pr.keys()):
                r = pr[board]
                lines.append(
                    f"      {r['from']}板→{r['to']}板：{r['prev_count']}只→晋级{r['promoted']}只 "
                    f"（晋级率{r['rate']:.1f}%）"
                )

    return "\n".join(lines) if lines else "今日无连板股"


def format_fund_flow(sectors: list, fund_flow_map: dict, top_n: int = 5) -> str:
    """格式化板块资金暗流（主力净额最极端的方向）"""
    if not sectors or not fund_flow_map:
        return "无板块资金流数据"

    ranked = []
    for s in sectors:
        name = s.get("name", "")
        ff = fund_flow_map.get(name, {})
        mf_net = ff.get("main_force_net", 0) or 0
        ranked.append((name, mf_net / 100000000))

    ranked.sort(key=lambda x: x[1], reverse=True)

    lines = []
    lines.append("  【主力猛攻】")
    for name, mf in ranked[:top_n]:
        lines.append(f"  {name}：主力净流入{mf:+.1f}亿")
    lines.append("  【主力撤退】")
    for name, mf in ranked[-top_n:]:
        lines.append(f"  {name}：主力净流入{mf:+.1f}亿")

    return "\n".join(lines)


def format_candidates(candidates: list, sector_code_map: dict = None) -> str:
    """格式化今日异动股（按驱动逻辑分类折叠，全量明细走 get_unusual_stocks FC）"""
    if not candidates:
        return "无候选股"

    categories = {
        "[涨停驱动]": [],
        "[资金异动]": [],
        "[强势突破]": [],
        "[龙虎榜加持]": [],
    }

    for c in candidates:
        tags = []
        if c.get("is_zt"):
            tags.append("[涨停驱动]")
        if c.get("lhb_net_yi") and abs(c["lhb_net_yi"]) > 0.1:
            tags.append("[龙虎榜加持]")
        if c.get("cons_boards", 0) >= 2:
            tags.append("[强势突破]")
        mf_wan = c.get("mf_net", 0) / 10000 if c.get("mf_net") else 0
        if abs(mf_wan) > 3000 and c.get("change", 0) < 9:
            tags.append("[资金异动]")

        assigned = False
        for cat in ["[涨停驱动]", "[龙虎榜加持]", "[强势突破]", "[资金异动]"]:
            if cat in tags:
                categories[cat].append(c)
                assigned = True
                break
        if not assigned:
            categories["[资金异动]"].append(c)

    lines = [f"共 {len(candidates)} 只"]
    for cat, recs in categories.items():
        if not recs:
            continue
        show = recs[:8]
        lines.append(f"  {cat}（共{len(recs)}只，展示{len(show)}只）：")
        for c in show:
            extra = []
            if c.get("is_zt"):
                boards = c.get("cons_boards", 0)
                extra.append(f"{'涨停' if boards <= 1 else str(boards) + '连板'}")
            if c.get("lhb_net_yi"):
                extra.append(f"龙虎榜净买{c['lhb_net_yi']:+.1f}亿")
            extra_str = f" [{', '.join(extra)}]" if extra else ""
            parts = []
            if c.get("industry"):
                parts.append(_sector_tag(c["industry"], sector_code_map))
            parts.extend(
                [
                    _sector_tag(concept, sector_code_map)
                    for concept in c.get("concepts", [])
                ]
            )
            sector_str = f" ({', '.join(parts)})" if parts else ""
            lines.append(
                f"  {c['code']} {c['name']}：涨{c['change']:+.1f}% "
                f"价{c.get('price', 0):.2f} "
                f"市值{c.get('mcap', 0):.0f}亿（流通{c.get('circ_mcap', 0):.0f}） "
                f"换手{c.get('turnover', 0):.1f}% 量比{c.get('vol_ratio', 0):.1f} "
                f"主力净{c.get('mf_net', 0) / 10000:+.0f}万({c.get('mf_ratio', 0):.1f}%)"
                f"{sector_str}{extra_str}"
            )
        if len(recs) > 8:
            lines.append(
                f"    ...等{len(recs) - 8}只（调用 get_unusual_stocks 查全量）"
            )
    return "\n".join(lines)


def format_strong_stocks(records: list, sector_code_map: dict = None) -> str:
    """格式化近期强势股（按趋势状态分类折叠）"""
    if not records:
        return "无强势股数据"

    categories = {"[趋势加速]": [], "[高位震荡]": [], "[退潮预警]": []}

    for r in records:
        angle = r.get("ma5_angle", 0) or 0
        change = r.get("change_pct", 0) or 0
        is_limit = r.get("is_limit_up", False)
        angle_threshold = 5

        if is_limit and angle > angle_threshold:
            categories["[趋势加速]"].append(r)
        elif angle <= 0 or (change < -2 and not is_limit):
            categories["[退潮预警]"].append(r)
        else:
            categories["[高位震荡]"].append(r)

    lines = [f"共 {len(records)} 只"]
    for cat, recs in categories.items():
        if not recs:
            continue
        show = recs[:8]
        lines.append(f"  {cat}（共{len(recs)}只，展示{len(show)}只）：")
        for i, r in enumerate(show, 1):
            change = r.get("change_pct", 0) or 0
            mcap = r.get("mcap", 0) or 0
            turnover = r.get("turnover_rate", 0) or 0
            mf = r.get("mf_wan", 0) or 0
            mf_ratio = r.get("main_force_ratio", 0) or 0
            concepts = r.get("concepts", [])
            concept_str = (
                f" [{', '.join(_sector_tag(c, sector_code_map) for c in concepts)}]"
                if concepts
                else ""
            )
            extra = []
            if r.get("is_limit_up"):
                extra.append("涨停")
            if r.get("limit_up_days"):
                extra.append(f"{r['limit_up_days']}天{r.get('limit_up_count', 0)}板")
            extra_str = f" [{', '.join(extra)}]" if extra else ""
            lines.append(
                f"  {i}. {r['stock_code']} {r['stock_name']}{concept_str}"
                f"  {change:+.2f}%  价{r.get('price', 0):.2f}  市值{mcap:.0f}亿"
                f"  换手{turnover:.1f}%  主力净{mf:+.0f}万({mf_ratio:.1f}%)"
                f"  MA5斜率={r.get('ma5_angle', 0):.1f}%{extra_str}"
            )
        if len(recs) > 8:
            lines.append(f"    ...等{len(recs) - 8}只")
    return "\n".join(lines)


def format_trend_stocks(data: dict, sector_code_map: dict = None) -> str:
    """
    格式化趋势股（双模式：5日线强趋势 + 20日线稳健趋势）

    Args:
        data: {'strong': [...], 'normal': [...]}
        sector_code_map: {sector_name: sector_code}
    """
    strong = data.get("strong", []) if data else []
    normal = data.get("normal", []) if data else []

    if not strong and not normal:
        return "今日无符合条件的趋势股"

    lines = []

    # --- 5日线强趋势 ---
    lines.append(f"\n【5日线强趋势 — 主升浪追涨型】共 {len(strong)} 只")
    lines.append(
        "  （条件：站上MA5, MA5>MA10>MA20, 偏离MA5<5%, MA5-MA20乖离>3%, 量能健康）"
    )
    if strong:
        for i, r in enumerate(strong, 1):
            parts = []
            if r.get("industry"):
                parts.append(_sector_tag(r["industry"], sector_code_map))
            sector_str = f" [{', '.join(parts)}]" if parts else ""
            lines.append(
                f"  {i}. {r['stock_code']} {r['stock_name']}{sector_str}"
                f"  得分{r['score']:.0f}  涨{r['change_pct']:+.2f}%"
                f"  价{r.get('price', 0):.2f}  市值{r['mcap']:.0f}亿  换手{r['turnover_rate']:.1f}%"
                f"  偏离MA5={r['bias_ma5']:+.1f}%"
                f"  MA5={r['ma5']:.2f} MA10={r['ma10']:.2f} MA20={r['ma20']:.2f}"
                f"  主力净{r['mf_wan']:+.0f}万"
            )
    else:
        lines.append("  （无符合条件的股票）")

    # --- 20日线稳健趋势 ---
    lines.append(f"\n【20日线稳健趋势 — 回调低吸型】共 {len(normal)} 只")
    lines.append(
        "  （条件：站上MA20, 偏离MA20<10%, MA5斜率>0, 量能健康, 排除已归入强趋势的股票）"
    )
    if normal:
        for i, r in enumerate(normal, 1):
            parts = []
            if r.get("industry"):
                parts.append(_sector_tag(r["industry"], sector_code_map))
            sector_str = f" [{', '.join(parts)}]" if parts else ""
            lines.append(
                f"  {i}. {r['stock_code']} {r['stock_name']}{sector_str}"
                f"  得分{r['score']:.0f}  涨{r['change_pct']:+.2f}%"
                f"  价{r.get('price', 0):.2f}  市值{r['mcap']:.0f}亿  换手{r['turnover_rate']:.1f}%"
                f"  偏离MA20={r['bias_ma20']:+.1f}%"
                f"  MA5={r['ma5']:.2f} MA10={r['ma10']:.2f} MA20={r['ma20']:.2f}"
                f"  主力净{r['mf_wan']:+.0f}万"
            )
    else:
        lines.append("  （无符合条件的股票）")

    return "\n".join(lines)


def format_yzt_performance(records: list, sector_code_map: dict = None) -> str:
    """格式化昨日涨停今日表现 — 摘要 + 精选（全量明细走 FC 查询）"""
    if not records:
        return "昨日无涨停股"
    total = len(records)
    positive = sum(1 for r in records if r.get("change", 0) > 0)
    positive_rate = positive / total * 100 if total > 0 else 0
    avg_change = sum(r.get("change", 0) for r in records) / total if total > 0 else 0

    lines = [
        f"  共{total}只，{positive}只溢价（{positive_rate:.0f}%），平均{avg_change:+.2f}%"
    ]
    down_5pct = [r for r in records if r.get("change", 0) <= -5]
    if down_5pct:
        worst = min(records, key=lambda r: r.get("change", 0))
        lines.append(
            f"  亏钱效应：{len(down_5pct)}只跌超5%，最深{worst['name']}({worst.get('code', '')}) {worst.get('change', 0):+.2f}%"
        )
    else:
        lines.append("  亏钱效应：无跌超5%个股，容错率较高")

    # 选股：连板成功 + 亏损TOP10（≤-3%），其余略过
    keep = set()
    for r in records:
        if r.get("boards", 0) >= 2 and r.get("change", 0) > 0:
            keep.add(r.get("code", ""))
    losers = [r for r in records if r.get("change", 0) <= -3]
    losers.sort(key=lambda r: r.get("change", 0))
    for r in losers[:10]:
        keep.add(r.get("code", ""))

    kept = [r for r in records if r.get("code", "") in keep]
    skipped = total - len(kept)

    lines.append(
        f"  精选{len(kept)}只（连板成功+亏损≤-3%TOP10），其余{skipped}只略"
        f"（可调用 get_yesterday_limit_ups 查询全量）\n"
    )

    for r in kept:
        boards = r.get("boards", 0)
        board_str = f" {boards}板" if boards >= 2 else ""
        mf_wan = r.get("mf_wan", 0) or 0
        mcap = r.get("mcap", 0) or 0
        parts = []
        if r.get("industry"):
            parts.append(_sector_tag(r["industry"], sector_code_map))
        sector_str = f" ({', '.join(parts)})" if parts else ""
        lines.append(
            f"  {r['name']}({r.get('code', '')}){board_str} 昨涨今{r['change']:+.2f}% "
            f"换手{r['turnover']:.1f}% 市值{mcap:.0f}亿 "
            f"主力净{mf_wan:+.0f}万({r['mf_ratio']:.1f}%)"
            f"{sector_str}"
        )

    return "\n".join(lines)


def format_hotspot(
    industries: list, concepts: list, sector_code_map: dict = None
) -> str:
    """格式化热点板块数据 — TOP5板块展示个股明细，其余仅摘要（明细走 FC 查询）"""
    lines = []
    DETAIL_LIMIT = 5

    def _format_sector_group(sectors, label):
        if not sectors:
            return
        show_detail = min(DETAIL_LIMIT, len(sectors))
        lines.append(f"【{label}板块热点 — TOP{show_detail}个股明细，其余摘要】")
        for i, s in enumerate(sectors, 1):
            # 板块总结行
            top_info = ""
            if s.get("top_stock"):
                top_info = f"｜领涨：{s['top_stock']}({s['top_stock_change']:+.1f}%)"
            seq_info = ""
            seq = s.get("change_seq", [])
            if seq and any(v is not None for v in seq):
                parts = [f"{v:+.2f}%" if v is not None else "-" for v in seq]
                seq_info = f"｜{' → '.join(parts)}"

            consec = s.get("consecutive_hot_days", 0) or 0
            trend = s.get("hot_trend", 0) or 0
            trend_labels = {1: "加速↑", -1: "退潮↓", 2: "持平→", 0: "新上榜"}
            trend_str = f"｜热度{trend_labels.get(trend, '')}" if trend else ""
            if consec >= 3:
                consec_str = f"｜⚠️连热{consec}天警惕兑现"
            elif consec >= 2:
                consec_str = f"｜连热{consec}天"
            else:
                consec_str = ""

            up = s.get("up_count", 0) or 0
            down = s.get("down_count", 0) or 0
            if up + down > 0:
                breadth_str = f"｜涨跌比{up}/{down}"
                if down > up:
                    breadth_str += "⚠️"
            else:
                breadth_str = ""

            # 板块 MA
            ma_str = ""
            ma5 = s.get("sector_ma5")
            ma20 = s.get("sector_ma20")
            if ma5 and ma20:
                ma_str = f"｜板块MA5={ma5:.2f} MA20={ma20:.2f}"
            # 排名变化
            rank_chg = s.get("rank_change")
            rank_str = ""
            if rank_chg is not None:
                if rank_chg > 0:
                    rank_str = f"｜⬆升{abs(rank_chg)}位"
                elif rank_chg < 0:
                    rank_str = f"｜⬇降{abs(rank_chg)}位"
                else:
                    rank_str = "｜排名持平"
            # 资金流 3 日趋势
            ff3d = s.get("fund_flow_3d", [])
            ff_str = ""
            if ff3d and len(ff3d) == 3 and any(v is not None for v in ff3d):
                parts = [f"{v:+.1f}亿" if v is not None else "-" for v in ff3d]
                ff_str = f"｜主力3日：{' → '.join(parts)}"

            lines.append(
                f"  {i}. {_sector_tag(s['name'], sector_code_map)}：涨{s['change']:+.2f}%｜"
                f"成分{s['total_count']}只｜涨停{s['limit_count']}家｜"
                f"主力净流入{s['main_force_net']:+.1f}亿｜"
                f"近10天{s['hot_days']}次进前10"
                f"{breadth_str}"
                f"{consec_str}"
                f"{trend_str}"
                f"{ma_str}"
                f"{rank_str}"
                f"{ff_str}"
                f"{top_info}{seq_info}"
            )

            # 仅 TOP5 展示个股明细
            if i > DETAIL_LIMIT:
                continue

            stocks = s.get("stocks", [])
            if not stocks:
                continue
            lines.append(f"      个股明细（得分排序，共{len(stocks)}只）：")
            for j, st in enumerate(stocks, 1):
                board_str = f" {st['boards']}板" if st.get("boards") else ""
                seal_str = f" 封{st['first_seal']}" if st.get("first_seal") else ""
                lines.append(
                    f"        {j}. {st['name']}({st['code']}){board_str} "
                    f"涨{st['change']:+.1f}% "
                    f"流通市值{st['circ_mcap']:.0f}亿 "
                    f"换手{st['turnover']:.1f}% "
                    f"主力净{st['mf_wan']:+.0f}万({st['mf_ratio']:.1f}%)"
                    f"{seal_str} "
                    f"MA5={st.get('ma5', 0):.2f} MA10={st.get('ma10', 0):.2f} MA20={st.get('ma20', 0):.2f} MA5斜率{st.get('ma5_angle', 0):.1f}% "
                    f"得分{st['stock_score']:.1f}"
                )
        lines.append("")

    _format_sector_group(industries, "行业")
    _format_sector_group(concepts, "概念")

    # 追加提示
    if len(industries) > DETAIL_LIMIT or len(concepts) > DETAIL_LIMIT:
        lines.append(
            f"  注：仅 TOP{DETAIL_LIMIT} 板块展示个股明细，其余板块个股可调用 get_hotspot_stocks 查询。"
        )

    return "\n".join(lines) if lines else "无热点数据"


def format_index_data(indices: list) -> str:
    """格式化主要指数表现（3 日完整 OHLC + 成交额 + MA5/MA10/MA20）"""
    if not indices:
        return "无指数数据"
    lines = []
    for idx in indices:
        name = idx.get("index_name", "?")
        close = idx.get("close", 0)
        open_p = idx.get("open", 0)
        high = idx.get("high", 0)
        low = idx.get("low", 0)
        chg = idx.get("change_percent", 0) or 0
        chg_amt = idx.get("change_amount", 0) or 0
        turnover = idx.get("turnover", 0) or 0
        ma5 = idx.get("ma5", 0)
        ma10 = idx.get("ma10", 0)
        ma20 = idx.get("ma20", 0)
        d1_close = idx.get("d1_close", 0)
        d1_open = idx.get("d1_open", 0)
        d1_high = idx.get("d1_high", 0)
        d1_low = idx.get("d1_low", 0)
        d1_chg = idx.get("d1_change", 0) or 0
        d1_turnover = idx.get("d1_turnover", 0) or 0
        d2_close = idx.get("d2_close", 0)
        d2_chg = idx.get("d2_change", 0) or 0
        d2_turnover = idx.get("d2_turnover", 0) or 0

        # MA 位置描述
        ma_str = ""
        if ma5 and ma10 and ma20:
            above_ma5 = ">" if close > ma5 else "<"
            above_ma20 = ">" if close > ma20 else "<"
            if close > ma5 > ma10 > ma20:
                ma_str = f"  MA5/MA10/MA20：{ma5:.2f}/{ma10:.2f}/{ma20:.2f}（多头排列）"
            elif close > ma20:
                ma_str = f"  MA5/MA10/MA20：{ma5:.2f}/{ma10:.2f}/{ma20:.2f}（价{above_ma5}MA5, 价{above_ma20}MA20）"
            else:
                ma_str = (
                    f"  MA5/MA10/MA20：{ma5:.2f}/{ma10:.2f}/{ma20:.2f}（价低于MA20）"
                )

        lines.append(
            f"  {name}：收盘{close:.2f} 开{open_p:.2f} 高{high:.2f} 低{low:.2f} "
            f"涨跌{chg:+.2f}%({chg_amt:+.2f}) 成交额{turnover:.0f}亿{ma_str}\n"
            f"    昨收{d1_close:.2f}({d1_chg:+.2f}%) 成交{d1_turnover:.0f}亿 "
            f"开{d1_open:.2f} 高{d1_high:.2f} 低{d1_low:.2f}\n"
            f"    前收{d2_close:.2f}({d2_chg:+.2f}%) 成交{d2_turnover:.0f}亿"
        )
    return "\n".join(lines)


def format_macro_overview(macro: dict) -> str:
    """格式化隔夜宏观数据"""
    if not macro or not macro.get("trade_date"):
        return "无宏观数据（今日早报尚未执行或数据缺失）"
    lines = []
    nasdaq = macro.get("nasdaq_change")
    kweb = macro.get("kweb_change")
    usd_cny = macro.get("usd_cny_rate")
    a50_price = macro.get("a50_price")
    a50_chg = macro.get("a50_change")
    oil = macro.get("crude_oil_price")
    oil_chg = macro.get("crude_oil_change")
    gold = macro.get("gold_price")
    gold_chg = macro.get("gold_change")

    if nasdaq is not None:
        lines.append(f"  纳指：{nasdaq:+.2f}%")
    if kweb is not None:
        lines.append(f"  中概股（KWEB）：{kweb:+.2f}%")
    if usd_cny is not None:
        lines.append(f"  美元/人民币（离岸）：{usd_cny:.4f}")
    if a50_price:
        a50_str = f"  A50期货：{a50_price:.0f}"
        if a50_chg is not None:
            a50_str += f"({a50_chg:+.2f}%)"
        lines.append(a50_str)
    if oil:
        oil_str = f"  WTI原油：${oil:.2f}"
        if oil_chg is not None:
            oil_str += f"({oil_chg:+.2f}%)"
        lines.append(oil_str)
    if gold:
        gold_str = f"  COMEX黄金：${gold:.2f}"
        if gold_chg is not None:
            gold_str += f"({gold_chg:+.2f}%)"
        lines.append(gold_str)

    return "\n".join(lines) if lines else "无宏观数据"


def format_risk_flags(items: list, candidates: list = None, label: str = "风险") -> str:
    """格式化风险标记（股东增减持/监管函/重点监控）"""
    if not items:
        return ""

    candidate_codes = set()
    if candidates:
        candidate_codes = {c.get("code", "") for c in candidates}

    lines = []
    for item in items:
        code = item.get("stock_code", "")
        name = item.get("stock_name", "")
        in_pool = " 在观察池" if code in candidate_codes else ""

        direction = item.get("change_direction", "") or label

        if direction in ("减持", "增持"):
            holder = item.get("holder_name", "")
            rate = item.get("change_rate", 0) or 0
            lines.append(
                f"  {code} {name}：{holder} {direction}{abs(rate):.2f}%{in_pool}"
            )
        elif label == "监管":
            title = (item.get("title", "") or "")[:60]
            issuer = item.get("issuer_short", "") or ""
            risk = item.get("risk_level", 0) or 0
            risk_type = item.get("risk_type", "") or ""
            summary = (
                item.get("risk_summary", "") or item.get("pdf_summary", "") or ""
            )[:80]
            lines.append(
                f"  {code} {name}：[{risk_type}] {issuer}{'☆' * risk} {title}{in_pool}"
            )
            if summary:
                lines.append(f"    摘要：{summary}")
        elif label == "监控":
            mtype = item.get("monitor_type", "")
            rule = (item.get("trigger_rule", "") or "")[:40]
            lines.append(f"  {code} {name}：[{mtype}] {rule}{in_pool}")

    if not lines:
        return ""
    return "\n".join(lines)


def format_announcements(announcements: list) -> str:
    """格式化重要公告"""
    if not announcements:
        return "今日无重要公告"

    lines = []
    for a in announcements[:30]:
        score = a.get("importance_score", 0)
        atype = a.get("announcement_type", "")
        title = a.get("announcement_title", "")[:60]
        code = a.get("stock_code", "")
        name = a.get("stock_name", "")
        lines.append(f"  [{atype}] {code} {name}：{title}（重要度{score:.0f}）")

    return "\n".join(lines)


def format_three_day_trend(trend: dict) -> str:
    """格式化 3 日趋势线（D-2 → D-1 → 今天）"""
    if not trend:
        return "无趋势数据"

    lines = []
    labels = [
        ("成交额（亿）", "turnover", "{:.0f}"),
        ("上涨占比", "up_ratio", "{:.2f}"),
        ("涨停", "limit_up", "{:.0f}"),
        ("封板率（%）", "seal_rate", "{:.1f}"),
        ("连板数", "chain_count", "{:.0f}"),
        ("最高板", "highest_board", "{:.0f}"),
        ("涨幅>5%", "up_5pct", "{:.0f}"),
        ("跌停", "limit_down", "{:.0f}"),
    ]
    for label, key, fmt in labels:
        vals = trend.get(key, [0, 0, 0])
        arrow = f"  {'→'.join(fmt.format(v) for v in vals)}"
        lines.append(f"  {label}：{arrow}")

    return "\n".join(lines)


def format_broken_boards(
    records: list, broken_trend: dict = None, sector_code_map: dict = None
) -> str:
    """格式化炸板明细 — 按行业分组展示"""
    if not records:
        return "今日无炸板"

    header = [f"共 {len(records)} 只"]
    if broken_trend:
        d2, d1, d = (
            broken_trend.get("d2", 0),
            broken_trend.get("d1", 0),
            broken_trend.get("d", 0),
        )
        header.append(f"（近3日：{d2} → {d1} → {d}）")
    lines = [" ".join(header)]

    # 按行业分组
    from collections import OrderedDict

    groups = OrderedDict()
    for r in records:
        ind = r.get("industry", "") or "未分类"
        if ind not in groups:
            groups[ind] = []
        groups[ind].append(r)

    # 按每组数量降序
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    for ind, recs in sorted_groups:
        ind_tag = _sector_tag(ind, sector_code_map) if ind and ind != "未分类" else ind
        lines.append(f"  [{ind_tag}]（{len(recs)}只）：")
        # 前 5 只详情
        for r in recs[:5]:
            open_cnt = f" 开板{r['open_count']}次" if r.get("open_count") else ""
            seal = f" 首次封板{r['first_seal']}" if r.get("first_seal") else ""
            lines.append(
                f"    {r['name']}({r['code']}) 收{r['change']:+.2f}%{open_cnt}{seal} "
                f"换手{r['turnover']:.1f}% 主力净{r['mf_wan']:+.0f}万 市值{r['mcap']:.0f}亿"
            )
        if len(recs) > 5:
            # 其余只列名称
            rest_names = [f"{r['name']}({r['code']})" for r in recs[5:]]
            lines.append(
                f"    ...等{len(recs) - 5}只：{'、'.join(rest_names[:10])}"
                + ("..." if len(rest_names) > 10 else "")
            )

    return "\n".join(lines)


def format_first_boards(records: list, sector_code_map: dict = None) -> str:
    """格式化首板苗子（早盘秒板 + 龙虎榜加持 + 封单比）"""
    if not records:
        return "无精选首板"
    lines = [f"共 {len(records)} 只（筛选条件：封板≤09:40 或 龙虎榜上榜）"]
    for i, r in enumerate(records, 1):
        parts = []
        if r.get("industry"):
            parts.append(_sector_tag(r["industry"], sector_code_map))
        parts.extend([_sector_tag(c, sector_code_map) for c in r.get("concepts", [])])
        ind = f" [{', '.join(parts)}]" if parts else ""
        seal = f" 封板{r['first_seal']}" if r.get("first_seal") else ""
        # 封单金额 + 封单比
        seal_amt = r.get("seal_amount", 0) or 0
        turnover_wan = r.get("turnover_wan", 0) or 0
        seal_amt_yi = seal_amt / 100000000
        seal_ratio = (seal_amt / 10000 / turnover_wan * 100) if turnover_wan > 0 else 0
        seal_str = f" 封板资金{seal_amt_yi:.1f}亿" if seal_amt_yi > 0 else ""
        seal_ratio_str = f" 封单比{seal_ratio:.1f}%" if seal_ratio > 0 else ""
        lhb = (
            f" 龙虎榜净买{r['lhb_net'] / 100000000:+.2f}亿" if r.get("lhb_net") else ""
        )
        extra = f"{seal}{seal_str}{seal_ratio_str}{lhb}"
        lines.append(
            f"  {i}. {r['name']}({r['code']}){ind} {r['change']:+.2f}% "
            f"换手{r['turnover']:.1f}% 量比{r['vol_ratio']:.1f} 振幅{r['amplitude']:.1f}% "
            f"市值{r['mcap']:.0f}亿（流通{r['circ_mcap']:.0f}） "
            f"主力净{r['mf_wan']:+.0f}万({r['mf_ratio']:.1f}%) "
            f"超大单{r['sl_wan']:+.0f}万 "
            f"价{r.get('price', 0):.2f} MA5={r.get('ma5', 0):.2f} MA10={r.get('ma10', 0):.2f} MA20={r.get('ma20', 0):.2f}"
        )
        if extra:
            lines.append(f"     [{extra}]")
    return "\n".join(lines)


def format_limit_quality(quality: dict) -> str:
    """格式化涨停质量细分"""
    if not quality:
        return "无数据"
    parts = []
    if quality.get("一字板", 0) > 0:
        parts.append(f"一字板{quality['一字板']}家")
    if quality.get("换手板", 0) > 0:
        parts.append(f"换手板{quality['换手板']}家")
    if quality.get("回封板", 0) > 0:
        parts.append(f"回封板{quality['回封板']}家")
    return "、".join(parts) if parts else "无涨停"


def format_capital_concentration(concentration: dict) -> str:
    """格式化资金集中度"""
    if not concentration:
        return "无数据"
    top3_pct = concentration.get("top3_pct", 0)
    total_inflow = concentration.get("total_inflow", 0)
    return f"主力净流入总额{total_inflow:+.1f}亿，TOP3占{top3_pct:.0f}%"


def format_lhb_full(lhb_data: list) -> str:
    """格式化龙虎榜 — 只保留核心指标+席位特征标签（席位明细走 FC 查询）"""
    if not lhb_data:
        return "今日无龙虎榜数据"

    # 总览统计
    total = len(lhb_data)
    total_net = sum(s.get("net_wan", 0) or 0 for s in lhb_data)
    inst_count = 0
    hm_count = 0
    for s in lhb_data:
        for seat in s.get("buy_seats", []) + s.get("sell_seats", []):
            name = seat.get("name", "")
            if "机构专用" in (name or ""):
                inst_count += 1
                break
            elif any(
                k in (name or "")
                for k in ("国泰海通", "华泰", "华鑫", "中信", "银河", "国盛")
            ):
                hm_count += 1
                break

    lines = [
        f"上榜：{total}只 | 净买+{total_net / 10000:.1f}亿 | 机构{inst_count}只 | 游资{hm_count}只",
        "",
    ]

    for s in lhb_data:
        code = s.get("code", "")
        name = s.get("name", "")
        change = s.get("change", 0) or 0
        net_wan = s.get("net_wan", 0) or 0
        net_ratio = (s.get("net_ratio", 0) or 0) * 100
        turnover_rate = s.get("turnover_rate", 0) or 0
        boards = s.get("boards", 0) or 0
        reason = s.get("reason", "")

        # 席位特征标签
        buy_seats = s.get("buy_seats", [])
        inst_buy = sum(
            seat["buy"] for seat in buy_seats if "机构专用" in (seat.get("name") or "")
        )
        total_buy = sum(seat["buy"] for seat in buy_seats) or 1
        inst_ratio = inst_buy / total_buy

        hm_count_local = sum(
            1
            for seat in buy_seats
            if any(
                k in (seat.get("name") or "")
                for k in ("国泰海通", "华泰", "华鑫", "中信", "银河", "国盛")
            )
        )

        tags = []
        if inst_ratio > 0.4:
            tags.append("机构主导")
        elif hm_count_local >= 3:
            tags.append("游资合力")
        elif net_wan < -3000:
            tags.append("资金出逃")
        elif abs(net_ratio) < 5 and total > 0:
            tags.append("分歧")

        if boards >= 3:
            tags.insert(0, f"{boards}连板")
        freq = s.get("lhb_freq", 1) or 1
        if freq > 1:
            tags.append(f"{freq}次上榜")

        tag_str = f" 【{'|'.join(tags)}】" if tags else ""

        line = f"{name}({code}) {change:+.2f}% 净买{net_wan / 10000:+.2f}亿({net_ratio:.1f}%)"
        if turnover_rate:
            line += f" 换手{turnover_rate:.1f}%"
        line += tag_str
        lines.append(line)

    return "\n".join(lines)


def calc_position_cap(
    limit_up: int,
    broken: int,
    highest_board: int,
    seal_rate: float,
    up_ratio: float,
    yzt_avg_change: float = 0,
    # 新增维度
    turnover_change: float = 0,
    up_5pct: int = 0,
    down_5pct: int = 0,
    limit_down: int = 0,
    index_ma_health: float = 50,
) -> int:
    """
    多维度加权评分计算仓位硬顶（0-100分 → 7 档仓位）

    9 个维度覆盖：涨停温度、封板率、高度、涨跌比、溢价率、
                   炸板率、量能、涨跌>5%分布、指数均线健康度
    """
    # 1. 涨停温度 (15%)：涨停家数越多，市场越热
    limit_up_score = min(limit_up / 100 * 100, 100)

    # 2. 封板率 (15%)：封板率代表资金信心
    seal_score = seal_rate

    # 3. 最高板 (10%)：高度代表空间想象力
    board_score = min(highest_board / 10 * 100, 100)

    # 4. 涨跌比 (10%)：市场宽度
    breadth_score = up_ratio * 100

    # 5. 昨日涨停溢价率 (10%)：赚钱效应持续性
    premium_score = max(0, min((yzt_avg_change + 5) / 15 * 100, 100))

    # 6. 炸板率 (10%)：亏钱效应
    total_touched = limit_up + broken
    broken_rate = (broken / total_touched * 100) if total_touched > 0 else 0
    broken_score = max(0, 100 - broken_rate * 2)

    # 7. 量能环比 (10%)：放量=增量资金进场，缩量=存量博弈
    if turnover_change >= 25:
        vol_score = 100
    elif turnover_change >= 15:
        vol_score = 85
    elif turnover_change >= 5:
        vol_score = 70
    elif turnover_change >= -5:
        vol_score = 55
    elif turnover_change >= -15:
        vol_score = 40
    elif turnover_change >= -25:
        vol_score = 20
    else:
        vol_score = 0

    # 8. 涨跌>5%分布 (10%)：强势股 vs 弱势股的宽度
    total_strong = up_5pct + down_5pct
    if total_strong > 0:
        strong_breadth_score = (up_5pct / total_strong) * 100
    else:
        strong_breadth_score = 50  # 无极端波动时中性

    # 9. 指数均线健康度 (10%)：多少个主要指数站稳 MA5/MA20
    ma_health_score = index_ma_health

    total_score = (
        limit_up_score * 0.15
        + seal_score * 0.15
        + board_score * 0.10
        + breadth_score * 0.10
        + premium_score * 0.10
        + broken_score * 0.10
        + vol_score * 0.10
        + strong_breadth_score * 0.10
        + ma_health_score * 0.10
    )

    # 跌停惩罚：跌停>30 时额外扣分，防止跌停潮中被其他维度拉回
    if limit_down >= 50:
        total_score -= 15
    elif limit_down >= 30:
        total_score -= 10
    elif limit_down >= 20:
        total_score -= 5

    total_score = max(0, total_score)

    # 7 档仓位
    if total_score >= 85:
        return 100
    elif total_score >= 70:
        return 80
    elif total_score >= 60:
        return 65
    elif total_score >= 50:
        return 50
    elif total_score >= 35:
        return 35
    elif total_score >= 20:
        return 25
    else:
        return 15


def fmt_change(val: float) -> str:
    """格式化变化量：+N / -N / 持平"""
    if val > 0:
        return f"+{val:.0f}"
    elif val < 0:
        return f"{val:.0f}"
    return "持平"


def fmt_pct_change(curr: float, prev: float) -> str:
    """格式化百分比变化"""
    if prev == 0:
        return "无对比"
    chg = (curr - prev) / prev * 100
    if chg > 0:
        return f"+{chg:.1f}%"
    elif chg < 0:
        return f"{chg:.1f}%"
    return "持平"


def safe_float(val, default=0):
    """安全转换为 float"""
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default
