"""Donde se guardan las filas.

Database no sabe si las tablas estan en memoria o en un archivo: le habla a un
Storage. Hay dos:

- MemoryStorage: todo en diccionarios de Python, se pierde al cerrar.
- DiskStorage: usa el motor escrito en Rust (minidb._engine), que guarda las
  tablas y los indices en B-trees dentro de un archivo.

Cada fila tiene un rowid, un numero interno que la identifica y que sirve para
decir que filas cambiar o borrar. Las filas se devuelven siempre ordenadas por
rowid, que es el orden en que se insertaron.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import ExecutionError
from .nodes import Value

Row = tuple[Value, ...]
# un extremo de una busqueda por indice: (valor, si se incluye)
Limit = tuple[Value, bool] | None
# (tipo, nombre, tabla, sql), con tipo "table" o "index"
CatalogEntry = tuple[str, str, str, str]


class Storage(Protocol):
    def catalog(self) -> list[CatalogEntry]: ...
    def create_table(self, name: str, sql: str) -> None: ...

    def create_index(self, name: str, table: str, column: int, sql: str) -> None:
        """Indice sobre la columna numero `column` de la tabla."""
        ...

    def drop_table(self, name: str) -> None:
        """Borra la tabla y sus indices."""
        ...

    def drop_index(self, name: str) -> None: ...

    def insert(self, table: str, rows: list[Row]) -> list[int]:
        """Devuelve el rowid de cada fila nueva."""
        ...

    def scan(self, table: str) -> list[tuple[int, Row]]: ...

    def search(self, index: str, low: Limit, high: Limit) -> list[tuple[int, Row]]:
        """Filas cuyo valor en la columna del indice esta entre low y high (sin NULLs)."""
        ...

    def update(self, table: str, changes: list[tuple[int, Row]]) -> None: ...
    def delete(self, table: str, rowids: list[int]) -> None: ...
    def clear(self, table: str) -> None: ...

    def commit(self) -> None:
        """Hace permanentes los cambios hechos hasta ahora."""
        ...

    def rollback(self) -> None:
        """Descarta los cambios hechos desde el ultimo commit."""
        ...

    def close(self) -> None: ...


@dataclass
class _MemoryIndex:
    table: str
    column: int
    sql: str


class MemoryStorage:
    """Los indices no tienen estructura propia: una busqueda recorre la tabla.
    Sirve para que los resultados sean los mismos que en disco.

    Para poder hacer rollback, la primera vez que algo cambia despues de un commit
    se guarda una copia de como estaba (del catalogo, o de las filas de esa tabla).
    """

    def __init__(self) -> None:
        self._schemas: dict[str, str] = {}
        self._rows: dict[str, dict[int, Row]] = {}
        self._indexes: dict[str, _MemoryIndex] = {}
        # como estaban las cosas en el ultimo commit (solo lo que cambio desde entonces)
        self._saved_catalog: tuple[dict[str, str], dict[str, _MemoryIndex]] | None = None
        self._saved_rows: dict[str, dict[int, Row] | None] = {}

    def _save_catalog(self) -> None:
        if self._saved_catalog is None:
            self._saved_catalog = (dict(self._schemas), dict(self._indexes))

    def _save_rows(self, table: str) -> None:
        if table not in self._saved_rows:
            rows = self._rows.get(table)
            self._saved_rows[table] = None if rows is None else dict(rows)

    def catalog(self) -> list[CatalogEntry]:
        tables = [("table", name, name, sql) for name, sql in self._schemas.items()]
        indexes = [("index", name, i.table, i.sql) for name, i in self._indexes.items()]
        return tables + indexes

    def create_table(self, name: str, sql: str) -> None:
        self._save_catalog()
        self._save_rows(name)
        self._schemas[name] = sql
        self._rows[name] = {}

    def create_index(self, name: str, table: str, column: int, sql: str) -> None:
        self._save_catalog()
        self._indexes[name] = _MemoryIndex(table, column, sql)

    def drop_table(self, name: str) -> None:
        self._save_catalog()
        self._save_rows(name)
        del self._schemas[name]
        del self._rows[name]
        for index in [n for n, i in self._indexes.items() if i.table == name]:
            del self._indexes[index]

    def drop_index(self, name: str) -> None:
        self._save_catalog()
        del self._indexes[name]

    def insert(self, table: str, rows: list[Row]) -> list[int]:
        self._save_rows(table)
        stored = self._rows[table]
        # como en disco: el siguiente al rowid mas grande (las claves estan en orden)
        first = next(reversed(stored), 0) + 1
        rowids = list(range(first, first + len(rows)))
        stored.update(zip(rowids, rows, strict=True))
        return rowids

    def scan(self, table: str) -> list[tuple[int, Row]]:
        return list(self._rows[table].items())

    def search(self, index: str, low: Limit, high: Limit) -> list[tuple[int, Row]]:
        info = self._indexes[index]

        def inside(value: Any) -> bool:
            if value is None:
                return False
            if low is not None and (value < low[0] or (value == low[0] and not low[1])):
                return False
            if high is not None and (value > high[0] or (value == high[0] and not high[1])):
                return False
            return True

        return [(r, row) for r, row in self._rows[info.table].items() if inside(row[info.column])]

    def update(self, table: str, changes: list[tuple[int, Row]]) -> None:
        self._save_rows(table)
        self._rows[table].update(changes)

    def delete(self, table: str, rowids: list[int]) -> None:
        self._save_rows(table)
        stored = self._rows[table]
        for rowid in rowids:
            stored.pop(rowid, None)

    def clear(self, table: str) -> None:
        self._save_rows(table)
        self._rows[table] = {}

    def commit(self) -> None:
        self._saved_catalog = None
        self._saved_rows = {}

    def rollback(self) -> None:
        if self._saved_catalog is not None:
            self._schemas, self._indexes = self._saved_catalog
        for table, rows in self._saved_rows.items():
            if rows is None:
                self._rows.pop(table, None)
            else:
                self._rows[table] = rows
        self.commit()

    def close(self) -> None:
        pass


class DiskStorage:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        try:
            from . import _engine
        except ImportError:
            raise ExecutionError(
                "el motor en Rust no esta compilado (instalalo con: pip install -e .)"
            ) from None
        self._errors = _engine.StorageError
        with self._translate():
            self._engine = _engine.Engine(os.fspath(path))

    @contextmanager
    def _translate(self) -> Iterator[None]:
        # para el resto de minidb los errores del motor son errores comunes de ejecucion
        try:
            yield
        except self._errors as e:
            raise ExecutionError(str(e)) from None

    def catalog(self) -> list[CatalogEntry]:
        with self._translate():
            return self._engine.catalog()

    def create_table(self, name: str, sql: str) -> None:
        with self._translate():
            self._engine.create_table(name, sql)

    def create_index(self, name: str, table: str, column: int, sql: str) -> None:
        with self._translate():
            self._engine.create_index(name, table, column, sql)

    def drop_table(self, name: str) -> None:
        with self._translate():
            self._engine.drop_table(name)

    def drop_index(self, name: str) -> None:
        with self._translate():
            self._engine.drop_index(name)

    def insert(self, table: str, rows: list[Row]) -> list[int]:
        with self._translate():
            return self._engine.insert(table, rows)

    def scan(self, table: str) -> list[tuple[int, Row]]:
        with self._translate():
            return self._engine.scan(table)

    def search(self, index: str, low: Limit, high: Limit) -> list[tuple[int, Row]]:
        with self._translate():
            return self._engine.search(index, low, high)

    def update(self, table: str, changes: list[tuple[int, Row]]) -> None:
        with self._translate():
            self._engine.update(table, changes)

    def delete(self, table: str, rowids: list[int]) -> None:
        with self._translate():
            self._engine.delete(table, rowids)

    def clear(self, table: str) -> None:
        with self._translate():
            self._engine.clear(table)

    def commit(self) -> None:
        with self._translate():
            self._engine.commit()

    def rollback(self) -> None:
        with self._translate():
            self._engine.rollback()

    def page_count(self) -> int:
        with self._translate():
            return self._engine.page_count()

    def close(self) -> None:
        self._engine.close()
