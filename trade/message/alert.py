"""消息推送 — 告警路由、去重冷却、SL提醒管理。"""

import time


class AlertManager:
    """消息推送管理器 — 群聊/私聊路由、指纹去重、冷却抑制。

    由 Watcher 持有实例，各领域模块通过它推送消息。
    """

    def __init__(self, telegram_bot=None, private_telegram=None):
        self.telegram = telegram_bot
        self._private = private_telegram
        self._fingerprints: dict[str, int] = {}  # fingerprint → scan_count
        self._cooldown: dict[str, tuple[int, float]] = {}  # code → (scan, price)

    def send(self, msg: str, private: bool = False):
        """发送消息（自动选通道）。"""
        if private and self._private:
            self._private.send(msg)
        elif self.telegram:
            self.telegram.send_message(msg)

    def should_throttle(self, code: str, price: float, scan_count: int,
                        min_interval: int = 7) -> bool:
        """检查是否应抑制该股票的重复推送。"""
        if code not in self._cooldown:
            return False
        last_scan, last_price = self._cooldown[code]
        if scan_count - last_scan < min_interval:
            return True
        if price > 0 and abs(price - last_price) / last_price < 0.005:
            return True
        return False

    def record_push(self, code: str, price: float, scan_count: int):
        """记录推送，用于去重判断。"""
        self._cooldown[code] = (scan_count, price)

    def is_fingerprint_seen(self, fingerprint: str, scan_count: int,
                            interval: int = 30) -> bool:
        """检查指纹是否在 interval 轮内已推送。"""
        last = self._fingerprints.get(fingerprint, -999)
        return scan_count - last < interval

    def record_fingerprint(self, fingerprint: str, scan_count: int):
        """记录指纹推送。"""
        self._fingerprints[fingerprint] = scan_count
