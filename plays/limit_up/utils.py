"""limit_up 玩法工具函数：类型转换、交易时间、数据格式"""

from datetime import datetime


def safe_float(val):
    """安全转换为float，失败返回0.0"""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def safe_float_none(val):
    """安全转换为float，失败返回None"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int_none(val):
    """安全转换为int，失败返回None"""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def is_trading_time():
    """判断当前是否在A股交易时间(9:30~11:30, 13:00~15:00，工作日)"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour >= 15:
        return False
    if now.hour == 11 and now.minute >= 30:
        return False
    if now.hour == 12:
        return False
    return True


def list_to_dict(items, fields):
    """将Tushare返回的list格式转为dict格式"""
    if not items or not fields:
        return []
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, (list, tuple)):
            d = {}
            for i, f in enumerate(fields):
                if i < len(item):
                    d[f] = item[i]
            result.append(d)
    return result