"""
股票代码工具函数

提供股票代码格式验证、标准化等功能
"""


def strip_stock_code(code):
    """
    去除股票代码后缀（.SH/.SZ/.BJ）

    Args:
        code: 股票代码（可能带后缀）

    Returns:
        str: 纯数字股票代码（6 位）
    """
    if not code:
        return code

    # 去除空格
    code = str(code).strip()

    # 去除后缀
    for suffix in [".SH", ".SZ", ".BJ", ".sh", ".sz", ".bj"]:
        if code.endswith(suffix):
            code = code[: -len(suffix)]
            break

    return code


def get_stock_suffix(stock_code):
    """
    根据股票代码前缀判断交易所后缀

    Args:
        stock_code: 股票代码（可以带或不带后缀）

    Returns:
        str: 交易所后缀 (.SH/.SZ/.BJ) 或 None
    """
    if not stock_code or len(stock_code) < 6:
        return None

    # 如果已经有后缀，直接返回
    if "." in stock_code:
        return None

    prefix = stock_code[:3]

    # 沪市 A 股
    if prefix in ["600", "601", "603", "605", "688"]:
        return ".SH"

    # 深市 A 股
    if prefix in ["000", "001", "002", "003", "300", "301"]:
        return ".SZ"

    # 北交所（包括 920 开头）
    if prefix.startswith("8") or prefix == "920":
        return ".BJ"

    # 沪市 B 股
    if prefix == "900":
        return ".SH"

    # 深市 B 股
    if prefix == "200":
        return ".SZ"

    # 沪市可转债
    if prefix in ["118", "113"]:
        return ".SH"

    # 深市可转债
    if prefix in ["123", "127"]:
        return ".SZ"

    # 无法识别
    return None


def validate_stock_code(code):
    """
    验证股票代码格式

    Args:
        code: 股票代码

    Returns:
        bool: 是否有效
    """
    if not code:
        return False

    # 必须带后缀
    if "." not in code:
        return False

    # 验证格式
    parts = code.split(".")
    if len(parts) != 2:
        return False

    code_part, suffix = parts

    # 代码部分必须是 6 位数字
    if not code_part.isdigit() or len(code_part) != 6:
        return False

    # 后缀必须是 SH/SZ/BJ
    if suffix not in ["SH", "SZ", "BJ"]:
        return False

    return True


def normalize_stock_code(code):
    """
    标准化股票代码（添加后缀）

    Args:
        code: 股票代码（可以带或不带后缀）

    Returns:
        str: 标准化后的代码（带后缀）
    """
    if not code:
        return code

    # 如果已有后缀，直接返回（转大写）
    if "." in code:
        return code.upper()

    # 添加后缀
    suffix = get_stock_suffix(code)
    if suffix:
        return code + suffix

    # 无法识别，返回原值
    return code
