import pytest

from calculator import modulo, power


def test_power():
    assert power(2, 10) == 1024


def test_modulo():
    assert modulo(10, 3) == 1


def test_modulo_by_zero():
    with pytest.raises(ZeroDivisionError):
        modulo(1, 0)
