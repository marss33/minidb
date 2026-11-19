"""Evaluacion de expresiones con la logica de tres valores de SQL.

NULL se representa con None. Cualquier comparacion o cuenta con NULL da NULL,
y en el WHERE solo pasan las filas donde la condicion es exactamente True.
"""

import operator
from collections.abc import Callable, Iterator
from typing import Any

from .errors import ExecutionError
from .nodes import Aggregate, BinaryOp, Column, Expr, IsNull, Literal, UnaryOp, Value

# dada una columna devuelve su valor en la fila actual. En una consulta con
# GROUP BY tambien calcula las funciones de agregacion sobre el grupo actual
Env = Callable[[Column | Aggregate], Value]

COMPARE: dict[str, Callable[[Any, Any], Any]] = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def type_name(value: Value) -> str:
    if value is None:
        return "NULL"
    if type(value) is bool:
        return "BOOL"
    if type(value) is int:
        return "INT"
    return "TEXT"


def evaluate(expr: Expr, env: Env) -> Value:
    match expr:
        case Literal(value):
            return value
        case Column() | Aggregate():
            return env(expr)
        case IsNull(operand, negated):
            return (evaluate(operand, env) is None) != negated
        case UnaryOp("NOT", operand):
            v = as_bool(evaluate(operand, env))
            return None if v is None else not v
        case UnaryOp("-", operand):
            n = evaluate(operand, env)
            return None if n is None else -as_int(n, "-")
        case BinaryOp("AND", left, right):
            return sql_and(as_bool(evaluate(left, env)), as_bool(evaluate(right, env)))
        case BinaryOp("OR", left, right):
            return sql_or(as_bool(evaluate(left, env)), as_bool(evaluate(right, env)))
        case BinaryOp(op, left, right):
            return binary(op, evaluate(left, env), evaluate(right, env))
    raise AssertionError(f"expresion desconocida: {expr!r}")


def columns_in(expr: Expr, *, inside_aggregates: bool = True) -> Iterator[Column]:
    """Todas las columnas que usa una expresion (para validar antes de recorrer filas)."""
    match expr:
        case Column():
            yield expr
        case Aggregate(_, arg):
            if arg is not None and inside_aggregates:
                yield from columns_in(arg)
        case UnaryOp(_, operand) | IsNull(operand, _):
            yield from columns_in(operand, inside_aggregates=inside_aggregates)
        case BinaryOp(_, left, right):
            yield from columns_in(left, inside_aggregates=inside_aggregates)
            yield from columns_in(right, inside_aggregates=inside_aggregates)


def aggregates_in(expr: Expr) -> Iterator[Aggregate]:
    match expr:
        case Aggregate():
            yield expr
        case UnaryOp(_, operand) | IsNull(operand, _):
            yield from aggregates_in(operand)
        case BinaryOp(_, left, right):
            yield from aggregates_in(left)
            yield from aggregates_in(right)


def render(expr: Expr) -> str:
    """La expresion como texto, para el nombre de una columna del resultado."""
    match expr:
        case Literal(None):
            return "NULL"
        case Literal(str() as text):
            return "'" + text.replace("'", "''") + "'"
        case Literal(value):
            return str(value)
        case Column():
            return str(expr)
        case Aggregate(func, arg):
            return f"{func.lower()}({'*' if arg is None else render(arg)})"
        case IsNull(operand, negated):
            return f"{render(operand)} IS {'NOT ' if negated else ''}NULL"
        case UnaryOp("NOT", operand):
            return f"NOT {render(operand)}"
        case UnaryOp(op, operand):
            return f"{op}{render(operand)}"
        case BinaryOp(op, left, right):
            parts = [
                f"({render(e)})" if isinstance(e, BinaryOp) else render(e) for e in (left, right)
            ]
            return f"{parts[0]} {op} {parts[1]}"
    raise AssertionError(f"expresion desconocida: {expr!r}")


def binary(op: str, a: Value, b: Value) -> Value:
    if a is None or b is None:
        return None

    if op in COMPARE:
        if type(a) is not type(b):
            raise ExecutionError(f"no se puede comparar {type_name(a)} con {type_name(b)}")
        return bool(COMPARE[op](a, b))

    x, y = as_int(a, op), as_int(b, op)
    if op == "+":
        return x + y
    if op == "-":
        return x - y
    if op == "*":
        return x * y
    if op == "/":
        if y == 0:
            raise ExecutionError("division por cero")
        # como en SQL, la division entera trunca hacia cero (// de python redondea para abajo)
        q = abs(x) // abs(y)
        return q if (x >= 0) == (y >= 0) else -q
    raise AssertionError(f"operador desconocido: {op}")


def sql_and(a: bool | None, b: bool | None) -> bool | None:
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def sql_or(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def as_int(value: Value, op: str) -> int:
    if type(value) is not int:
        raise ExecutionError(f"'{op}' necesita numeros, no {type_name(value)}")
    return value


def as_bool(value: Value) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise ExecutionError(f"se esperaba una condicion y vino {type_name(value)}")
