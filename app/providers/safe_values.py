"""Provider 层数值清洗：外部脏值统一转 None（见 PRD 第 24 节）。"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal


def safe_float(value) -> float | None:
    """将 '-'、''、None、NaN、inf 及不可解析值统一转换为 None。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"-", "--", "nan", "NaN", "None"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, bool):  # bool 是 int 子类，单独排除
        return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None

    if math.isnan(number) or math.isinf(number):
        return None
    return number


def safe_int(value) -> int | None:
    """将 '-'、''、None、NaN 及不可解析值统一转换为 None（etf-data-module）。"""
    number = safe_float(value)
    return None if number is None else int(number)


def safe_str(value) -> str | None:
    """将 None、空字符串、常见空占位符统一转换为 None（etf-data-module）。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text if text and text not in {"-", "--", "nan", "NaN", "None"} else None
    return str(value) or None


def safe_date(value) -> date | None:
    """将 datetime/date/字符串统一转换为 date，空值归 None（etf-data-module）。

    支持格式：
    - datetime 对象：提取 .date()
    - date 对象：直接返回
    - 字符串 YYYY-MM-DD / YYYYMMDD：解析为 date
    - None / 空字符串 / 不可解析：返回 None
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"-", "--", "nan", "NaN", "None", "NaT"}:
            return None
        # YYYY-MM-DD 格式
        if "-" in text:
            try:
                parts = text.split("-")
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            except (ValueError, IndexError):
                return None
        # YYYYMMDD 格式
        if len(text) == 8 and text.isdigit():
            try:
                return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                return None
    return None
