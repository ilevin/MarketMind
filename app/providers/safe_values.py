"""Provider 层数值清洗：外部脏值统一转 None（见 PRD 第 24 节）。"""

from __future__ import annotations

import math
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
