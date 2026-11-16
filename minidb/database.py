"""Ejecuta las sentencias sobre tablas guardadas en memoria.

Por ahora cada tabla es una lista de filas. Mas adelante esto se reemplaza
por paginas en disco y un B-tree, pero la interfaz de Database queda igual.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import assert_never

from .errors import ExecutionError
from .expressions import Env, as_bool, columns_in, evaluate, type_name
from .nodes import (
    ColumnDef,
    CreateTable,
    Delete,
    DropTable,
    Expr,
    Insert,
    Select,
    Statement,
    Update,
    Value,
)
from .parser import parse

Row = list[Value]


@dataclass
class Table:
    name: str
    columns: tuple[ColumnDef, ...]
    rows: list[Row] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._positions = {col.name: i for i, col in enumerate(self.columns)}

    def index_of(self, column: str) -> int:
        try:
            return self._positions[column]
        except KeyError:
            raise ExecutionError(f"la tabla {self.name} no tiene la columna {column}") from None

    def env(self, row: Row) -> Env:
        return lambda name: row[self.index_of(name)]

    def schema(self) -> str:
        cols = ", ".join(f"{c.name} {c.type}" for c in self.columns)
        return f"CREATE TABLE {self.name} ({cols})"


@dataclass
class Result:
    columns: tuple[str, ...] = ()
    rows: list[tuple[Value, ...]] = field(default_factory=list)
    message: str = ""


class Database:
    def __init__(self) -> None:
        self.tables: dict[str, Table] = {}

    def execute(self, sql: str) -> list[Result]:
        return [self.run(stmt) for stmt in parse(sql)]

    def query(self, sql: str) -> list[tuple[Value, ...]]:
        """Atajo: ejecuta y devuelve las filas del ultimo resultado."""
        results = self.execute(sql)
        return results[-1].rows if results else []

    def run(self, stmt: Statement) -> Result:
        match stmt:
            case CreateTable():
                return self._create(stmt)
            case DropTable():
                self._table(stmt.table)
                del self.tables[stmt.table]
                return Result(message=f"tabla {stmt.table} borrada")
            case Insert():
                return self._insert(stmt)
            case Select():
                return self._select(stmt)
            case Update():
                return self._update(stmt)
            case Delete():
                return self._delete(stmt)
            case _:
                assert_never(stmt)

    def _table(self, name: str) -> Table:
        try:
            return self.tables[name]
        except KeyError:
            raise ExecutionError(f"no existe la tabla {name}") from None

    def _create(self, stmt: CreateTable) -> Result:
        if stmt.table in self.tables:
            raise ExecutionError(f"la tabla {stmt.table} ya existe")
        names = [c.name for c in stmt.columns]
        if len(set(names)) != len(names):
            raise ExecutionError("hay columnas con el mismo nombre")
        self.tables[stmt.table] = Table(stmt.table, stmt.columns)
        return Result(message=f"tabla {stmt.table} creada")

    def _insert(self, stmt: Insert) -> Result:
        table = self._table(stmt.table)
        if stmt.columns is None:
            targets = list(range(len(table.columns)))
        else:
            targets = [table.index_of(c) for c in stmt.columns]
            if len(set(targets)) != len(targets):
                raise ExecutionError("hay columnas repetidas en el INSERT")

        # primero se arman todas las filas y recien despues se guardan,
        # asi si una falla no queda el insert por la mitad
        new_rows: list[Row] = []
        for values in stmt.rows:
            if len(values) != len(targets):
                raise ExecutionError(
                    f"se esperaban {len(targets)} valores y vinieron {len(values)}"
                )
            row: Row = [None] * len(table.columns)
            for i, expr in zip(targets, values, strict=True):
                row[i] = check_type(table.columns[i], evaluate(expr, no_columns))
            new_rows.append(row)

        table.rows.extend(new_rows)
        return Result(message=rows_message(len(new_rows), "insertada"))

    def _select(self, stmt: Select) -> Result:
        table = self._table(stmt.table)
        if stmt.columns is None:
            indexes = list(range(len(table.columns)))
        else:
            indexes = [table.index_of(c) for c in stmt.columns]

        keep = self._predicate(table, stmt.where)
        rows = [row for row in table.rows if keep(row)]

        # ordenar de la ultima clave a la primera funciona porque sort() es estable
        for item in reversed(stmt.order_by):
            rows.sort(key=sort_key(table.index_of(item.column)), reverse=item.descending)

        if stmt.limit is not None:
            rows = rows[: stmt.limit]

        names = tuple(table.columns[i].name for i in indexes)
        return Result(names, [tuple(row[i] for i in indexes) for row in rows])

    def _update(self, stmt: Update) -> Result:
        table = self._table(stmt.table)
        targets = [(table.index_of(col), expr) for col, expr in stmt.assignments]
        for _, expr in targets:
            validate_columns(expr, table)

        keep = self._predicate(table, stmt.where)
        rows = [row for row in table.rows if keep(row)]

        # igual que en el insert: se calcula todo antes de tocar nada
        changes = []
        for row in rows:
            env = table.env(row)
            changes.append(
                [(i, check_type(table.columns[i], evaluate(expr, env))) for i, expr in targets]
            )
        for row, row_changes in zip(rows, changes, strict=True):
            for i, value in row_changes:
                row[i] = value

        return Result(message=rows_message(len(rows), "actualizada"))

    def _delete(self, stmt: Delete) -> Result:
        table = self._table(stmt.table)
        doomed = self._predicate(table, stmt.where)
        before = len(table.rows)
        table.rows = [row for row in table.rows if not doomed(row)]
        return Result(message=rows_message(before - len(table.rows), "borrada"))

    def _predicate(self, table: Table, where: Expr | None) -> Callable[[Row], bool]:
        if where is None:
            return lambda row: True
        validate_columns(where, table)
        return lambda row: as_bool(evaluate(where, table.env(row))) is True


def check_type(column: ColumnDef, value: Value) -> Value:
    if value is None:
        return None
    expected = int if column.type == "INT" else str
    if type(value) is not expected:
        raise ExecutionError(
            f"la columna {column.name} es {column.type} y se le paso {type_name(value)}"
        )
    return value


def validate_columns(expr: Expr, table: Table) -> None:
    for name in columns_in(expr):
        table.index_of(name)


def no_columns(name: str) -> Value:
    raise ExecutionError(f"no se pueden usar columnas en VALUES ({name})")


def sort_key(index: int) -> Callable[[Row], tuple[int, Value]]:
    # los NULL van primero, y asi nunca se compara None contra un numero
    return lambda row: (0, 0) if row[index] is None else (1, row[index])


def rows_message(n: int, verb: str) -> str:
    return f"1 fila {verb}" if n == 1 else f"{n} filas {verb}s"
