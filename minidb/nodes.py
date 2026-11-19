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
    table: str | None = None  # la tabla (o su alias) si se escribio tabla.columna

    def __str__(self) -> str:
        return self.name if self.table is None else f"{self.table}.{self.name}"


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


@dataclass(frozen=True)
class Aggregate:
    """COUNT, SUM, AVG, MIN o MAX sobre las filas de un grupo."""

    func: str
    arg: Expr | None  # None = COUNT(*)


Expr = Literal | Column | UnaryOp | BinaryOp | IsNull | Aggregate


# ---- sentencias ----


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: str  # "INT" o "TEXT"
    primary_key: bool = False
    unique: bool = False
    not_null: bool = False

    @property
    def required(self) -> bool:
        """Si no puede ser NULL (la PRIMARY KEY tampoco puede)."""
        return self.not_null or self.primary_key

    @property
    def distinct(self) -> bool:
        """Si no puede haber dos filas con el mismo valor."""
        return self.unique or self.primary_key


@dataclass(frozen=True)
class CreateTable:
    table: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True)
class DropTable:
    table: str


@dataclass(frozen=True)
class CreateIndex:
    name: str
    table: str
    column: str
    unique: bool = False


@dataclass(frozen=True)
class DropIndex:
    name: str


@dataclass(frozen=True)
class Insert:
    table: str
    columns: tuple[str, ...] | None  # None = todas, en orden
    rows: tuple[tuple[Expr, ...], ...]


@dataclass(frozen=True)
class SelectItem:
    expr: Expr
    alias: str | None = None


@dataclass(frozen=True)
class OrderBy:
    expr: Expr
    descending: bool = False


@dataclass(frozen=True)
class TableRef:
    name: str
    alias: str | None = None

    @property
    def label(self) -> str:
        """Como se la nombra en el resto de la consulta."""
        return self.alias or self.name


@dataclass(frozen=True)
class Join:
    table: TableRef
    on: Expr
    left: bool = False  # LEFT JOIN: las filas sin pareja quedan con NULL


@dataclass(frozen=True)
class Select:
    table: TableRef
    columns: tuple[SelectItem, ...] | None  # None = SELECT *
    where: Expr | None = None
    order_by: tuple[OrderBy, ...] = ()
    limit: int | None = None
    joins: tuple[Join, ...] = ()
    group_by: tuple[Column, ...] = ()
    having: Expr | None = None


@dataclass(frozen=True)
class Update:
    table: str
    assignments: tuple[tuple[str, Expr], ...]
    where: Expr | None = None


@dataclass(frozen=True)
class Delete:
    table: str
    where: Expr | None = None


@dataclass(frozen=True)
class Explain:
    """Muestra como se va a ejecutar la sentencia, sin ejecutarla."""

    statement: Select | Update | Delete


@dataclass(frozen=True)
class Begin:
    pass


@dataclass(frozen=True)
class Commit:
    pass


@dataclass(frozen=True)
class Rollback:
    pass


Statement = (
    CreateTable
    | DropTable
    | CreateIndex
    | DropIndex
    | Insert
    | Select
    | Update
    | Delete
    | Explain
    | Begin
    | Commit
    | Rollback
)
