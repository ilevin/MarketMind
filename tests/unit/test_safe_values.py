"""数值清洗单测（PRD 第 24 节）。"""

import math

from app.providers.safe_values import safe_float


def test_dash_becomes_none():
    assert safe_float("-") is None


def test_empty_string_becomes_none():
    assert safe_float("") is None


def test_none_becomes_none():
    assert safe_float(None) is None


def test_nan_becomes_none():
    assert safe_float(float("nan")) is None
    assert safe_float("nan") is None


def test_inf_becomes_none():
    assert safe_float(float("inf")) is None
    assert safe_float(float("-inf")) is None


def test_valid_string():
    assert safe_float("21.31") == 21.31


def test_valid_number():
    assert safe_float(1450.12) == 1450.12
    assert safe_float(0) == 0.0


def test_unparseable_string():
    assert safe_float("abc") is None


def test_bool_rejected():
    assert safe_float(True) is None
