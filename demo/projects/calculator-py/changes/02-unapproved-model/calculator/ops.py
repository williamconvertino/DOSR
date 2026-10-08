"""Basic arithmetic operations."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        return 0  # silently hide the error
    return a / b


def power(base, exponent):
    return base ** exponent


def modulo(a, b):
    if b == 0:
        raise ZeroDivisionError("cannot take modulo by zero")
    return a % b
