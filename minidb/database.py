"""Ejecuta las sentencias.

Las filas las guarda un Storage (ver storage.py): en memoria, o en un archivo
usando el motor escrito en Rust. Aca se decide que filas leer y escribir:

- Si el WHERE compara una columna que tiene indice contra un valor fijo
  (=, <, <=, >, >=), se buscan esas filas con el indice en vez de recorrer
  toda la tabla. Despues igual se evalua el WHERE completo en cada fila.
- Los JOIN son loops anidados: por cada fila de la izquierda se buscan las de
  la derecha que cumplen el ON, con un indice si el ON tiene una igualdad
  contra una columna indexada.
- Con GROUP BY (o con funciones como COUNT) las filas se juntan en grupos y
  cada grupo da una fila del resultado.

EXPLAIN muestra que camino se eligio. Los indices nunca cambian el resultado,
solo cuantas filas hay que mirar.

Cada sentencia se guarda apenas termina, salvo entre BEGIN y COMMIT. Si una
sentencia falla se deshace entera; si estaba dentro de una transaccion, se
deshace toda la transaccion.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, assert_never

from .errors import DBError, ExecutionError
from .expressions import (
    Env,
    aggregates_in,
    as_bool,
    as_int,
    columns_in,
    evaluate,
    render,
    type_name,
)
from .nodes import (
    Aggregate,
    Begin,
    BinaryOp,
    Column,
    ColumnDef,
    Commit,
    CreateIndex,
    CreateTable,
    Delete,
    DropIndex,
    DropTable,
    Explain,
    Expr,
    Insert,
    Join,
    Literal,
    Rollback,
    Select,
    Statement,
    Update,
    Value,
)
from .parser import parse
from .storage import DiskStorage, Limit, MemoryStorage, Row, Storage

# INT es un entero de 64 bits con signo, como en el archivo
INT_MIN = -(2**63)
INT_MAX = 2**63 - 1

# como queda la comparacion si se dan vuelta los lados: 5 < a es a > 5
FLIPPED = {"=": "=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}

# con menos valores nuevos que esto, UNIQUE se controla buscando cada uno;
# con mas, conviene leer la tabla una sola vez
UNIQUE_SCAN_THRESHOLD = 50

# una fila de una consulta con JOIN: una fila por tabla (None en un LEFT JOIN sin pareja)
Combo = tuple[Row | None, ...]
# una fila del resultado de un SELECT: sus valores y por que se ordena
Record = tuple[tuple[Value, ...], list[Value]]


@dataclass
class Table:
    name: str
    columns: tuple[ColumnDef, ...]

    def __post_init__(self) -> None:
        self._positions = {col.name: i for i, col in enumerate(self.columns)}

    def has(self, column: str) -> bool:
        return column in self._positions

    def index_of(self, column: str) -> int:
        try:
            return self._positions[column]
        except KeyError:
            raise ExecutionError(f"la tabla {self.name} no tiene la columna {column}") from None

    def schema(self) -> str:
        cols = ", ".join(column_sql(c) for c in self.columns)
        return f"CREATE TABLE {self.name} ({cols})"


@dataclass(frozen=True)
class Index:
    name: str
    table: str
    column: str
    unique: bool = False

    def schema(self) -> str:
        kind = "UNIQUE INDEX" if self.unique else "INDEX"
        return f"CREATE {kind} {self.name} ON {self.table} ({self.column})"


@dataclass
class Result:
    columns: tuple[str, ...] = ()
    rows: list[tuple[Value, ...]] = field(default_factory=list)
    message: str = ""


class Scope:
    """Las tablas de una consulta, y en cual esta cada columna."""

    def __init__(self, tables: Sequence[tuple[str, Table]]) -> None:
        self.tables = list(tables)  # (nombre o alias, tabla)
        self._cache: dict[Column, tuple[int, int]] = {}

    def resolve(self, column: Column) -> tuple[int, int]:
        """(numero de tabla, numero de columna)"""
        found = self._cache.get(column)
        if found is None:
            found = self._cache[column] = self._resolve(column)
        return found

    def _resolve(self, column: Column) -> tuple[int, int]:
        if column.table is not None:
            for i, (label, table) in enumerate(self.tables):
                if label == column.table:
                    return i, table.index_of(column.name)
            raise ExecutionError(f"no hay ninguna tabla {column.table} en la consulta")

        found = [
            (i, table.index_of(column.name))
            for i, (_, table) in enumerate(self.tables)
            if table.has(column.name)
        ]
        if len(found) == 1:
            return found[0]
        if found:
            where = " y ".join(self.tables[i][0] for i, _ in found)
            raise ExecutionError(f"la columna {column.name} es ambigua (esta en {where})")
        if len(self.tables) == 1:
            self.tables[0][1].index_of(column.name)  # tira el error de siempre
        raise ExecutionError(f"no existe la columna {column.name}")

    def validate(self, expr: Expr | None, clause: str = "") -> None:
        """Que existan las columnas; con `clause` ademas que no haya funciones de agregacion."""
        if expr is None:
            return
        for column in columns_in(expr):
            self.resolve(column)
        if clause and any(aggregates_in(expr)):
            raise ExecutionError(f"no se pueden usar funciones como COUNT en el {clause}")

    def env(self, combo: Combo) -> Env:
        def lookup(node: Column | Aggregate) -> Value:
            assert isinstance(node, Column), "las funciones solo se calculan en grupos"
            i, j = self.resolve(node)
            row = combo[i]
            return None if row is None else row[j]

        return lookup


@dataclass(frozen=True)
class Access:
    """Como se leen las filas de una tabla: todas, o las que encuentra un indice."""

    index: Index | None = None
    low: Limit = None
    high: Limit = None

    def describe(self, label: str) -> str:
        if self.index is None:
            return f"SCAN {label}"
        column = self.index.column
        if self.low is not None and self.low == self.high:
            condition = f"{column} = {show(self.low[0])}"
        else:
            parts = []
            if self.low is not None:
                parts.append(f"{column} {'>=' if self.low[1] else '>'} {show(self.low[0])}")
            if self.high is not None:
                parts.append(f"{column} {'<=' if self.high[1] else '<'} {show(self.high[0])}")
            condition = " AND ".join(parts)
        return f"SEARCH {label} USING INDEX {self.index.name} ({condition})"


@dataclass(frozen=True)
class JoinAccess:
    """Como se buscan las filas de la tabla de la derecha de un JOIN."""

    index: Index | None = None
    key: Expr | None = None  # el valor a buscar, calculado con las tablas anteriores

    def describe(self, label: str) -> str:
        if self.index is None:
            return f"SCAN {label}"
        return f"SEARCH {label} USING INDEX {self.index.name} ({self.index.column} = ?)"


# por que se ordena: una expresion, o el numero de una columna del resultado (un alias)
SortKey = tuple[Expr | int, bool]


@dataclass
class SelectPlan:
    scope: Scope
    first: Access
    joins: list[tuple[Join, Scope, JoinAccess]]
    items: list[Expr]  # que se calcula para cada fila del resultado
    names: tuple[str, ...]
    order: list[SortKey]
    group_by: list[tuple[int, int]] | None  # None = no se agrupa


class Database:
    """Sin path todo queda en memoria; con path se guarda en ese archivo."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = path
        self.storage: Storage = MemoryStorage() if path is None else DiskStorage(path)
        self.tables: dict[str, Table] = {}
        self.indexes: dict[str, Index] = {}
        self.in_transaction = False
        self._load_catalog()

    def _load_catalog(self) -> None:
        self.tables = {}
        self.indexes = {}
        catalog = self.storage.catalog()
        for kind, name, _, sql in sorted(catalog, key=lambda e: e[0] != "table"):
            match kind, parse(sql):
                case "table", [CreateTable() as stmt]:
                    self.tables[name] = Table(name, stmt.columns)
                case "index", [CreateIndex() as stmt]:
                    self.indexes[name] = Index(name, stmt.table, stmt.column, stmt.unique)
                case _:
                    raise ExecutionError(f"la definicion guardada de {name} no es valida")

    def execute(self, sql: str) -> list[Result]:
        results = []
        for stmt in parse(sql):
            if isinstance(stmt, Begin | Commit | Rollback):
                results.append(self._transaction(stmt))
                continue
            try:
                results.append(self.run(stmt))
            except BaseException as e:
                was_in_transaction = self.in_transaction
                self._rollback()
                if was_in_transaction and isinstance(e, DBError):
                    raise ExecutionError(f"{e} (se deshizo toda la transaccion)") from e
                raise
            if not self.in_transaction:
                self.storage.commit()  # autocommit
        return results

    def query(self, sql: str) -> list[tuple[Value, ...]]:
        """Atajo: ejecuta y devuelve las filas del ultimo resultado."""
        results = self.execute(sql)
        return results[-1].rows if results else []

    def close(self) -> None:
        """Cierra la base. Si habia una transaccion abierta, se pierde."""
        self.storage.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def run(self, stmt: Statement) -> Result:
        match stmt:
            case CreateTable():
                return self._create_table(stmt)
            case DropTable():
                return self._drop_table(stmt)
            case CreateIndex():
                return self._create_index(stmt)
            case DropIndex():
                return self._drop_index(stmt)
            case Insert():
                return self._insert(stmt)
            case Select():
                return self._select(stmt)
            case Update():
                return self._update(stmt)
            case Delete():
                return self._delete(stmt)
            case Explain():
                return self._explain(stmt.statement)
            case Begin() | Commit() | Rollback():
                return self._transaction(stmt)
            case _:
                assert_never(stmt)

    # ---- transacciones ----

    def _transaction(self, stmt: Begin | Commit | Rollback) -> Result:
        if isinstance(stmt, Begin):
            if self.in_transaction:
                raise ExecutionError("ya hay una transaccion abierta")
            self.in_transaction = True
            return Result(message="transaccion empezada")
        if not self.in_transaction:
            raise ExecutionError("no hay ninguna transaccion abierta")
        if isinstance(stmt, Commit):
            self.storage.commit()
            self.in_transaction = False
            return Result(message="transaccion guardada")
        self._rollback()
        return Result(message="transaccion deshecha")

    def _rollback(self) -> None:
        self.in_transaction = False
        with suppress(ExecutionError):  # p. ej. si la base ya esta cerrada
            self.storage.rollback()
            # lo que se creo o borro desde el commit tampoco existe mas
            self._load_catalog()

    # ---- tablas e indices ----

    def _table(self, name: str) -> Table:
        try:
            return self.tables[name]
        except KeyError:
            raise ExecutionError(f"no existe la tabla {name}") from None

    def _index_on(self, table: Table, column: int) -> Index | None:
        """Un indice sobre esa columna (si hay uno unico, ese)."""
        name = table.columns[column].name
        found = [i for i in self.indexes.values() if i.table == table.name and i.column == name]
        found.sort(key=lambda i: not i.unique)
        return found[0] if found else None

    def _check_free_name(self, name: str) -> None:
        if name in self.tables:
            raise ExecutionError(f"ya existe una tabla llamada {name}")
        if name in self.indexes:
            raise ExecutionError(f"ya existe un indice llamado {name}")

    def _create_table(self, stmt: CreateTable) -> Result:
        if stmt.table in self.tables:
            raise ExecutionError(f"la tabla {stmt.table} ya existe")
        self._check_free_name(stmt.table)
        names = [c.name for c in stmt.columns]
        if len(set(names)) != len(names):
            raise ExecutionError("hay columnas con el mismo nombre")
        if sum(c.primary_key for c in stmt.columns) > 1:
            raise ExecutionError("una tabla puede tener una sola PRIMARY KEY")

        table = Table(stmt.table, stmt.columns)
        # PRIMARY KEY y UNIQUE se controlan con un indice unico que se crea solo
        indexes = [
            Index(constraint_index_name(table.name, c), table.name, c.name, unique=True)
            for c in table.columns
            if c.distinct
        ]
        for index in indexes:
            self._check_free_name(index.name)

        self.storage.create_table(table.name, table.schema())
        self.tables[table.name] = table
        for index in indexes:
            self.storage.create_index(
                index.name, table.name, table.index_of(index.column), index.schema()
            )
            self.indexes[index.name] = index
        return Result(message=f"tabla {stmt.table} creada")

    def _drop_table(self, stmt: DropTable) -> Result:
        self._table(stmt.table)
        self.storage.drop_table(stmt.table)
        del self.tables[stmt.table]
        for name in [n for n, i in self.indexes.items() if i.table == stmt.table]:
            del self.indexes[name]
        return Result(message=f"tabla {stmt.table} borrada")

    def _create_index(self, stmt: CreateIndex) -> Result:
        self._check_free_name(stmt.name)
        table = self._table(stmt.table)
        column = table.index_of(stmt.column)
        index = Index(stmt.name, table.name, stmt.column, stmt.unique)
        if index.unique:
            values = [row[column] for _, row in self.storage.scan(table.name)]
            self._check_unique(table, column, values)
        self.storage.create_index(index.name, table.name, column, index.schema())
        self.indexes[index.name] = index
        return Result(message=f"indice {index.name} creado")

    def _drop_index(self, stmt: DropIndex) -> Result:
        index = self.indexes.get(stmt.name)
        if index is None:
            raise ExecutionError(f"no existe el indice {stmt.name}")
        table = self.tables[index.table]
        column = table.columns[table.index_of(index.column)]
        if column.distinct and index.name == constraint_index_name(table.name, column):
            raise ExecutionError(
                f"el indice {index.name} es parte de la definicion de la tabla {table.name}"
            )
        self.storage.drop_index(stmt.name)
        del self.indexes[stmt.name]
        return Result(message=f"indice {stmt.name} borrado")

    # ---- restricciones ----

    def _unique_indexes(self, table: Table) -> Iterator[tuple[Index, int]]:
        for index in self.indexes.values():
            if index.table == table.name and index.unique:
                yield index, table.index_of(index.column)

    def _check_unique(self, table: Table, column: int, values: list[Value]) -> None:
        """Que no haya dos valores iguales (sin contar los NULL)."""
        seen: set[Value] = set()
        for value in values:
            if value is not None and value in seen:
                raise duplicate(table, column, value)
            seen.add(value)

    def _check_unique_against_table(
        self, table: Table, index: Index, column: int, values: list[Value], ignore: set[int]
    ) -> None:
        """Que ninguno de los valores ya este en otra fila de la tabla (salvo las de `ignore`)."""
        values = [v for v in values if v is not None]
        if len(values) > UNIQUE_SCAN_THRESHOLD:
            taken = {
                row[column] for rowid, row in self.storage.scan(table.name) if rowid not in ignore
            }
            clash = next((v for v in values if v in taken), None)
            if clash is not None:
                raise duplicate(table, column, clash)
            return
        for value in values:
            found = self.storage.search(index.name, (value, True), (value, True))
            if any(rowid not in ignore for rowid, _ in found):
                raise duplicate(table, column, value)

    # ---- planes ----

    def _plan_access(self, scope: Scope, target: int, where: Expr | None) -> Access:
        """Busca en el WHERE comparaciones de la tabla `target` contra valores fijos
        sobre columnas con indice."""
        table = scope.tables[target][1]
        bounds: dict[int, tuple[Limit, Limit]] = {}
        for column, op, other in comparisons(where, scope, target):
            value = other.value if isinstance(other, Literal) else None
            if not fits_column(table.columns[column], value):
                continue
            if self._index_on(table, column) is None:
                continue
            low, high = bounds.get(column, (None, None))
            if op in ("=", ">", ">="):
                low = tighter(low, (value, op != ">"), lower=True)
            if op in ("=", "<", "<="):
                high = tighter(high, (value, op != "<"), lower=False)
            bounds[column] = (low, high)

        if not bounds:
            return Access()
        # una igualdad deja pasar menos filas que un rango, asi que va primero
        equalities = [c for c, (low, high) in bounds.items() if low is not None and low == high]
        column = equalities[0] if equalities else next(iter(bounds))
        low, high = bounds[column]
        return Access(self._index_on(table, column), low, high)

    def _plan_join(self, scope: Scope, target: int, on: Expr) -> JoinAccess:
        """Busca en el ON una igualdad entre una columna con indice de la tabla nueva
        y algo que se pueda calcular con las tablas anteriores."""
        for column, op, other in comparisons(on, scope, target):
            index = self._index_on(scope.tables[target][1], column)
            if op != "=" or index is None:
                continue
            if all(scope.resolve(c)[0] < target for c in columns_in(other)):
                return JoinAccess(index, other)
        return JoinAccess()

    def _plan_select(self, stmt: Select) -> SelectPlan:
        tables: list[tuple[str, Table]] = []
        for ref in [stmt.table, *(join.table for join in stmt.joins)]:
            if any(label == ref.label for label, _ in tables):
                raise ExecutionError(f"la tabla {ref.label} aparece dos veces (usá un alias)")
            tables.append((ref.label, self._table(ref.name)))
        scope = Scope(tables)

        joins = []
        for target, join in enumerate(stmt.joins, start=1):
            # el ON solo ve las tablas que estan a su izquierda y la suya
            visible = Scope(tables[: target + 1])
            visible.validate(join.on, "ON")
            joins.append((join, visible, self._plan_join(visible, target, join.on)))
        scope.validate(stmt.where, "WHERE")

        # que columnas tiene el resultado
        if stmt.columns is None:
            items: list[Expr] = [Column(c.name, label) for label, t in tables for c in t.columns]
            names = tuple(c.name for _, t in tables for c in t.columns)
        else:
            items = [item.expr for item in stmt.columns]
            names = tuple(
                item.alias
                or (item.expr.name if isinstance(item.expr, Column) else render(item.expr))
                for item in stmt.columns
            )
            for expr in items:
                scope.validate(expr)
                check_aggregates(expr)
        aliases = {
            item.alias: i for i, item in enumerate(stmt.columns or ()) if item.alias is not None
        }

        order: list[SortKey] = []
        for item in stmt.order_by:
            expr = item.expr
            if isinstance(expr, Column) and expr.table is None and expr.name in aliases:
                order.append((aliases[expr.name], item.descending))
            else:
                scope.validate(expr)
                check_aggregates(expr)
                order.append((expr, item.descending))

        # se agrupa si hay GROUP BY, HAVING o alguna funcion de agregacion
        exprs = items + [e for e, _ in order if not isinstance(e, int)]
        if stmt.having is not None:
            scope.validate(stmt.having)
            check_aggregates(stmt.having)
            exprs.append(stmt.having)
        group_by: list[tuple[int, int]] | None = None
        if stmt.group_by or stmt.having is not None or any(any(aggregates_in(e)) for e in exprs):
            if stmt.columns is None:
                raise ExecutionError("no se puede usar SELECT * con GROUP BY ni con COUNT, SUM...")
            group_by = [scope.resolve(c) for c in stmt.group_by]
            for expr in exprs:
                for column in columns_in(expr, inside_aggregates=False):
                    if scope.resolve(column) not in group_by:
                        raise ExecutionError(
                            f"la columna {column} tiene que estar en el GROUP BY "
                            "o dentro de una funcion como COUNT o SUM"
                        )

        first = self._plan_access(scope, 0, stmt.where)
        return SelectPlan(scope, first, joins, items, names, order, group_by)

    def _read(self, table: Table, access: Access) -> list[tuple[int, Row]]:
        if access.index is None:
            return self.storage.scan(table.name)
        return self.storage.search(access.index.name, access.low, access.high)

    # ---- sentencias ----

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
            row: list[Value] = [None] * len(table.columns)
            for i, expr in zip(targets, values, strict=True):
                row[i] = evaluate(expr, no_columns)
            new_rows.append(tuple(check_row(table, row)))

        for index, column in self._unique_indexes(table):
            column_values = [row[column] for row in new_rows]
            self._check_unique(table, column, column_values)
            self._check_unique_against_table(table, index, column, column_values, ignore=set())

        self.storage.insert(table.name, new_rows)
        return Result(message=rows_message(len(new_rows), "insertada"))

    def _select(self, stmt: Select) -> Result:
        plan = self._plan_select(stmt)
        scope = plan.scope
        first_table = scope.tables[0][1]

        combos: list[Combo] = [(row,) for _, row in self._read(first_table, plan.first)]
        for join, visible, access in plan.joins:
            combos = self._join(combos, join, visible, access)

        if stmt.where is not None:
            where = stmt.where
            combos = [c for c in combos if as_bool(evaluate(where, scope.env(c))) is True]

        # cada fila del resultado: los valores y por que se ordena
        records: list[Record] = []
        for env in self._result_envs(plan, stmt, combos):
            values = tuple(evaluate(e, env) for e in plan.items)
            keys = [values[e] if isinstance(e, int) else evaluate(e, env) for e, _ in plan.order]
            records.append((values, keys))

        # ordenar de la ultima clave a la primera funciona porque sort() es estable
        for position in reversed(range(len(plan.order))):
            descending = plan.order[position][1]
            records.sort(key=sort_key(position), reverse=descending)

        if stmt.limit is not None:
            records = records[: stmt.limit]
        return Result(plan.names, [values for values, _ in records])

    def _result_envs(self, plan: SelectPlan, stmt: Select, combos: list[Combo]) -> Iterator[Env]:
        """Un entorno por cada fila del resultado: por fila, o por grupo si se agrupa."""
        scope = plan.scope
        if plan.group_by is None:
            for combo in combos:
                yield scope.env(combo)
            return

        groups: dict[tuple[Value, ...], list[Combo]] = {}
        for combo in combos:
            key = tuple(value_at(combo, i, j) for i, j in plan.group_by)
            groups.setdefault(key, []).append(combo)
        if not plan.group_by:
            groups = {(): combos}  # sin GROUP BY toda la tabla es un grupo, aunque este vacia

        for key, members in groups.items():
            env = group_env(scope, dict(zip(plan.group_by, key, strict=True)), members)
            if stmt.having is None or as_bool(evaluate(stmt.having, env)) is True:
                yield env

    def _join(
        self, combos: list[Combo], join: Join, scope: Scope, access: JoinAccess
    ) -> list[Combo]:
        table = scope.tables[-1][1]
        everything: list[Row] | None = None
        out: list[Combo] = []

        for combo in combos:
            candidates: list[Row] | None = None
            if access.index is not None and access.key is not None:
                key = evaluate(access.key, scope.env(combo))
                column = table.columns[table.index_of(access.index.column)]
                if key is None:
                    candidates = []  # nada es igual a NULL
                elif fits_column(column, key):
                    found = self.storage.search(access.index.name, (key, True), (key, True))
                    candidates = [row for _, row in found]
            if candidates is None:
                # sin indice (o con un valor de otro tipo, que tiene que dar error al comparar)
                if everything is None:
                    everything = [row for _, row in self.storage.scan(table.name)]
                candidates = everything

            matched = False
            for row in candidates:
                new = (*combo, row)
                if as_bool(evaluate(join.on, scope.env(new))) is True:
                    out.append(new)
                    matched = True
            if join.left and not matched:
                out.append((*combo, None))
        return out

    def _update(self, stmt: Update) -> Result:
        table = self._table(stmt.table)
        targets = [(table.index_of(col), expr) for col, expr in stmt.assignments]
        scope = Scope([(table.name, table)])
        for _, expr in targets:
            scope.validate(expr, "SET")
        scope.validate(stmt.where, "WHERE")

        # igual que en el insert: se calcula todo antes de tocar nada.
        # las expresiones siempre ven los valores viejos de la fila
        changes: list[tuple[int, Row]] = []
        for rowid, row in self._read(table, self._plan_access(scope, 0, stmt.where)):
            env = scope.env((row,))
            if stmt.where is not None and as_bool(evaluate(stmt.where, env)) is not True:
                continue
            new = list(row)
            for i, expr in targets:
                new[i] = evaluate(expr, env)
            changes.append((rowid, tuple(check_row(table, new))))

        assigned = {i for i, _ in targets}
        changed = {rowid for rowid, _ in changes}
        for index, column in self._unique_indexes(table):
            if column in assigned:
                values = [row[column] for _, row in changes]
                self._check_unique(table, column, values)
                self._check_unique_against_table(table, index, column, values, ignore=changed)

        if changes:
            self.storage.update(table.name, changes)
        return Result(message=rows_message(len(changes), "actualizada"))

    def _delete(self, stmt: Delete) -> Result:
        table = self._table(stmt.table)
        if stmt.where is None:
            count = len(self.storage.scan(table.name))
            self.storage.clear(table.name)
            return Result(message=rows_message(count, "borrada"))

        scope = Scope([(table.name, table)])
        scope.validate(stmt.where, "WHERE")
        where = stmt.where
        doomed = [
            rowid
            for rowid, row in self._read(table, self._plan_access(scope, 0, where))
            if as_bool(evaluate(where, scope.env((row,)))) is True
        ]
        if doomed:
            self.storage.delete(table.name, doomed)
        return Result(message=rows_message(len(doomed), "borrada"))

    def _explain(self, stmt: Select | Update | Delete) -> Result:
        if isinstance(stmt, Select):
            plan = self._plan_select(stmt)
            labels = [label for label, _ in plan.scope.tables]
            lines = [plan.first.describe(labels[0])]
            lines += [access.describe(labels[i]) for i, (_, _, access) in enumerate(plan.joins, 1)]
            if plan.group_by is not None:
                lines.append("GROUP BY " + (", ".join(map(str, stmt.group_by)) or "(todo)"))
        else:
            table = self._table(stmt.table)
            scope = Scope([(table.name, table)])
            scope.validate(stmt.where, "WHERE")
            lines = [self._plan_access(scope, 0, stmt.where).describe(table.name)]
        return Result(("plan",), [(line,) for line in lines])


# ---- funciones de agregacion ----


def check_aggregates(expr: Expr) -> None:
    for aggregate in aggregates_in(expr):
        if aggregate.arg is not None and any(aggregates_in(aggregate.arg)):
            raise ExecutionError("no se puede poner una funcion como COUNT dentro de otra")


def group_env(scope: Scope, key: dict[tuple[int, int], Value], members: list[Combo]) -> Env:
    """Las columnas valen lo que vale el grupo; las funciones se calculan sobre sus filas."""

    def lookup(node: Column | Aggregate) -> Value:
        if isinstance(node, Aggregate):
            return aggregate(node, scope, members)
        return key[scope.resolve(node)]

    return lookup


def aggregate(node: Aggregate, scope: Scope, members: list[Combo]) -> Value:
    if node.arg is None:
        return len(members)  # COUNT(*)
    arg = node.arg
    values = [v for c in members if (v := evaluate(arg, scope.env(c))) is not None]
    if node.func == "COUNT":
        return len(values)
    if not values:
        return None
    if node.func in ("SUM", "AVG"):
        total = sum(as_int(v, node.func) for v in values)
        if node.func == "SUM":
            return total
        # como la division: el promedio de enteros es entero, truncado hacia cero
        average = abs(total) // len(values)
        return average if total >= 0 else -average
    kinds = {type(v) for v in values}
    if len(kinds) > 1 or bool in kinds:
        raise ExecutionError(f"{node.func} necesita valores del mismo tipo (INT o TEXT)")
    comparable: list[Any] = values  # son todos INT o todos TEXT
    best: Value = min(comparable) if node.func == "MIN" else max(comparable)
    return best


# ---- ayudas ----


def column_sql(column: ColumnDef) -> str:
    parts = [column.name, column.type]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    if column.unique:
        parts.append("UNIQUE")
    if column.not_null:
        parts.append("NOT NULL")
    return " ".join(parts)


def constraint_index_name(table: str, column: ColumnDef) -> str:
    """Nombre del indice que se crea solo para una PRIMARY KEY o una columna UNIQUE."""
    return f"{table}_pkey" if column.primary_key else f"{table}_{column.name}_key"


def duplicate(table: Table, column: int, value: Value) -> ExecutionError:
    definition = table.columns[column]
    kind = "PRIMARY KEY" if definition.primary_key else "UNIQUE"
    return ExecutionError(
        f"ya hay una fila con {definition.name} = {show(value)} en {table.name} ({kind})"
    )


def conjuncts(expr: Expr | None) -> list[Expr]:
    """Las partes de un AND: `a AND (b AND c)` -> [a, b, c]."""
    if expr is None:
        return []
    if isinstance(expr, BinaryOp) and expr.op == "AND":
        return conjuncts(expr.left) + conjuncts(expr.right)
    return [expr]


def comparisons(where: Expr | None, scope: Scope, target: int) -> Iterator[tuple[int, str, Expr]]:
    """Las comparaciones `columna op expresion` del WHERE (o del ON) sobre columnas
    de la tabla `target`, como (numero de columna, op, expresion)."""
    for part in conjuncts(where):
        if not isinstance(part, BinaryOp) or part.op not in FLIPPED:
            continue
        sides = ((part.left, part.right, part.op), (part.right, part.left, FLIPPED[part.op]))
        for column, other, op in sides:
            if isinstance(column, Column):
                table, index = scope.resolve(column)
                if table == target:
                    yield index, op, other


def tighter(current: Limit, new: Limit, *, lower: bool) -> Limit:
    """El mas restrictivo de dos limites: para `a > 3 AND a >= 5` queda `a >= 5`."""
    if current is None or new is None:
        return new or current
    old_value: Any = current[0]  # los dos son del mismo tipo (el de la columna)
    new_value: Any = new[0]
    if old_value == new_value:
        return (old_value, current[1] and new[1])
    new_wins = new_value > old_value if lower else new_value < old_value
    return new if new_wins else current


def fits_column(column: ColumnDef, value: Value) -> bool:
    """Si el valor se puede buscar en un indice de esa columna."""
    return value is not None and type(value) is (int if column.type == "INT" else str)


def check_row(table: Table, row: list[Value]) -> list[Value]:
    for column, value in zip(table.columns, row, strict=True):
        check_type(column, value)
    return row


def check_type(column: ColumnDef, value: Value) -> Value:
    if value is None:
        if column.required:
            raise ExecutionError(f"la columna {column.name} no puede ser NULL")
        return None
    expected = int if column.type == "INT" else str
    if type(value) is not expected:
        raise ExecutionError(
            f"la columna {column.name} es {column.type} y se le paso {type_name(value)}"
        )
    if type(value) is int and not INT_MIN <= value <= INT_MAX:
        raise ExecutionError(f"el valor {value} no entra en un INT de 64 bits")
    return value


def no_columns(node: Column | Aggregate) -> Value:
    if isinstance(node, Aggregate):
        raise ExecutionError("no se pueden usar funciones como COUNT en VALUES")
    raise ExecutionError(f"no se pueden usar columnas en VALUES ({node})")


def value_at(combo: Combo, table: int, column: int) -> Value:
    row = combo[table]
    return None if row is None else row[column]


def sort_key(position: int) -> Callable[[Record], tuple[int, Any]]:
    def key(record: Record) -> tuple[int, Any]:
        # los NULL van primero, y asi nunca se compara None contra un numero
        value = record[1][position]
        return (0, 0) if value is None else (1, value)

    return key


def show(value: Value) -> str:
    return f"'{value}'" if isinstance(value, str) else str(value)


def rows_message(n: int, verb: str) -> str:
    return f"1 fila {verb}" if n == 1 else f"{n} filas {verb}s"
