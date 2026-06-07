"""系统级消息路由 — 群聊/私聊 + 去重冷却 + 格式化。

可被任何模块独立使用：Watcher 用 alert() 做高频告警，
morning/strategy/review 用 send() 发一次性报告。
"""


class AlertRouter:
    """消息路由器 — 去重、冷却、通道选择。

    使用方式:
        router = AlertRouter(group_bot, private_bot)
        router.new_round(scan_count)          # Watcher 每轮调用
        router.alert("⚠️ 止损触发", code="000001")  # 带去重
        router.send("📋 早盘报告...")           # 不经过滤
    """

    def __init__(self, group_bot=None, private_bot=None):
        self._group = group_bot
        self._private = private_bot
        self._scan_count = 0
        # 指纹去重: {fingerprint: last_scan_count}
        self._seen: dict[str, int] = {}
        # 冷却: {code: (last_scan, last_price)}
        self._cooldown: dict[str, tuple[int, float]] = {}

    def new_round(self, scan_count: int):
        """每轮扫描开始时调用，更新轮次。"""
        self._scan_count = scan_count

    # ── 基础发送 ──

    def send(self, msg: str, channel: str = "group"):
        """发送消息，不经过滤。channel: group / private / both。"""
        if channel in ("group", "both") and self._group:
            self._group.send_message(msg)
        if channel in ("private", "both") and self._private:
            self._private.send(msg)

    # ── 带去重的告警（Watcher 高频场景）──

    def alert(
        self,
        msg: str,
        *,
        fingerprint: str | None = None,
        code: str | None = None,
        price: float = 0,
        cooldown_rounds: int = 7,
        fingerprint_rounds: int = 30,
        channel: str = "group",
    ) -> bool:
        """发送告警，自动去重和冷却。

        - fingerprint: 消息指纹。相同指纹在 fingerprint_rounds 轮内不重复推送
        - code: 股票代码。同代码在 cooldown_rounds 轮内且价格变化 <0.5% 时抑制
        - 返回是否实际发送

        去重规则:
        1. fingerprint 已见过 → 跳过
        2. code 在冷却期 + 价格变化 <0.5% → 跳过
        """
        scan = self._scan_count

        # 指纹去重
        if fingerprint:
            last = self._seen.get(fingerprint, -999)
            if scan - last < fingerprint_rounds:
                return False
            self._seen[fingerprint] = scan

        # 冷却去重
        if code and code in self._cooldown:
            last_scan, last_price = self._cooldown[code]
            if scan - last_scan < cooldown_rounds:
                return False
            if price > 0 and last_price > 0 and abs(price - last_price) / last_price < 0.005:
                return False

        if code and price > 0:
            self._cooldown[code] = (scan, price)

        self.send(msg, channel=channel)
        return True

    def alert_private(self, msg: str, **kwargs) -> bool:
        """发送私聊告警（实盘确认等）。"""
        return self.alert(msg, channel="private", **kwargs)

    # ── 查询 ──

    def is_cooling(self, code: str, rounds: int = 7) -> bool:
        """检查股票是否在冷却期。"""
        if code not in self._cooldown:
            return False
        return self._scan_count - self._cooldown[code][0] < rounds
