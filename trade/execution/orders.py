"""QMT 下单接口 — 预留"""


class OrderClient:
    def __init__(self, client=None):
        pass

    def buy(self, code: str, price: float, volume: int) -> dict:
        raise NotImplementedError("待 QMT 下单接口实测后实现")

    def sell(self, code: str, price: float, volume: int) -> dict:
        raise NotImplementedError("待 QMT 下单接口实测后实现")

    def cancel(self, order_id: str) -> dict:
        raise NotImplementedError("待 QMT 下单接口实测后实现")
