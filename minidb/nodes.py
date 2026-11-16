"""Nodos del arbol sintactico (AST) que arma el parser."""

from __future__ import annotations

from dataclasses import dataclass

Value = int | str | bool | None


# ---- expresiones ----


@dataclass(frozen=True)
class Literal:
    value: Value


@dataclass(frozen=True)
class Column:
    name: str


@dataclass(frozen=True)
class UnaryOp:
    op: str  # "NOT" o "-"
    operand: Expr


@dataclass(frozen=True)
class BinaryOp:
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class IsNull:
    operand: Expr
    negated: bool = False


Expr = Literal | Column | UnaryOp | BinaryOp | IsNull


# ---- sentencias ----


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: str  # "INT" o "TEXT"


@dataclass(frozen=True)
class CreateTable:
    table: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True)
class DropTable:
    table: str


@dataclass(frozen=True)
class Insert:
    table: str
    columns: tuple[str, ...] | None  # None = todas, en orden
    rows: tuple[tuple[Expr, ...], ...]


@dataclass(frozen=True)
class OrderBy:
    column: str
    descending: bool = False


@dataclass(frozen=True)
class Select:
    table: str
    columns: tuple[str, ...] | None  # None = SELECT *
    where: Expr | None = None
    order_by: tuple[OrderBy, ...] = ()
    limit: int | None = None


@dataclass(frozen=True)
class Update:
    table: str
    assignments: tuple[tuple[str, Expr], ...]
    where: Expr | None = None


@dataclass(frozen=True)
class Delete:
    table: str
    where: Expr | None = None


Statement = CreateTable | DropTable | Insert | Select | Update | Delete
