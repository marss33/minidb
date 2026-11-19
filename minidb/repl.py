"""Consola interactiva.

python -m minidb                       base en memoria
python -m minidb --db datos.db         base guardada en datos.db
python -m minidb [--db datos.db] script.sql   ejecuta el script y sale
"""

import argparse
from pathlib import Path

from . import __version__
from .database import Database, Result
from .errors import DBError
from .nodes import Value

PROMPT = "minidb> "
PROMPT_IN_TRANSACTION = "minidb*> "  # entre BEGIN y COMMIT
CONTINUATION = "   ...> "

HELP = """\
.tables          lista las tablas
.indexes         lista los indices
.schema TABLA    muestra como se creo una tabla y sus indices
.help            muestra esto
.exit            sale

Las consultas terminan con ; y pueden ocupar varias lineas.
Con EXPLAIN antes de un SELECT, UPDATE o DELETE se ve si usa un indice.
Entre BEGIN y COMMIT nada se guarda hasta el COMMIT (el prompt cambia a minidb*>)."""


def show(value: Value) -> str:
    if value is None:
        return "NULL"
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def format_result(result: Result) -> str:
    if not result.columns:
        return result.message

    body = [[show(v) for v in row] for row in result.rows]
    widths = [
        max([len(name)] + [len(row[i]) for row in body]) for i, name in enumerate(result.columns)
    ]
    # los numeros alineados a la derecha
    numeric = [any(type(row[i]) is int for row in result.rows) for i in range(len(result.columns))]

    def line(cells: list[str]) -> str:
        parts = [
            c.rjust(w) if num else c.ljust(w)
            for c, w, num in zip(cells, widths, numeric, strict=True)
        ]
        return "| " + " | ".join(parts) + " |"

    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [border, line(list(result.columns)), border]
    out += [line(row) for row in body]
    out.append(border)
    n = len(result.rows)
    out.append("(1 fila)" if n == 1 else f"({n} filas)")
    return "\n".join(out)


def run_sql(db: Database, sql: str) -> None:
    try:
        for result in db.execute(sql):
            print(format_result(result))
    except DBError as e:
        print(f"error: {e}")


def meta_command(db: Database, command: str) -> bool:
    """Devuelve False si hay que salir."""
    name, _, arg = command.partition(" ")
    arg = arg.strip().lower()

    if name in (".exit", ".quit"):
        return False
    if name == ".help":
        print(HELP)
    elif name == ".tables":
        print("\n".join(sorted(db.tables)) or "(no hay tablas)")
    elif name == ".indexes":
        lines = [f"{i.name} ({i.table}.{i.column})" for i in db.indexes.values()]
        print("\n".join(sorted(lines)) or "(no hay indices)")
    elif name == ".schema":
        if arg and arg not in db.tables:
            print(f"no existe la tabla {arg}")
            return True
        tables = [db.tables[arg]] if arg else list(db.tables.values())
        if not tables:
            print("(no hay tablas)")
        for table in tables:
            print(table.schema() + ";")
            for index in sorted(db.indexes.values(), key=lambda i: i.name):
                if index.table == table.name:
                    print(index.schema() + ";")
    else:
        print(f"comando desconocido: {name} (probá .help)")
    return True


def repl(db: Database) -> None:
    where = f"guardando en {db.path}" if db.path is not None else "en memoria"
    print(f"minidb {__version__} ({where}) - escribí .help para ver los comandos")
    buffer: list[str] = []

    while True:
        try:
            # en windows a veces llega un BOM invisible al principio
            prompt = PROMPT_IN_TRANSACTION if db.in_transaction else PROMPT
            line = input(CONTINUATION if buffer else prompt).lstrip("﻿")
        except KeyboardInterrupt:
            # ctrl+c cancela la consulta a medio escribir, o sale si no hay nada
            print()
            if buffer:
                buffer.clear()
                continue
            return
        except EOFError:
            print()
            return

        if not buffer:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("."):
                if not meta_command(db, stripped):
                    return
                continue

        buffer.append(line)
        sql = "\n".join(buffer)
        if sql.rstrip().endswith(";"):
            buffer.clear()
            run_sql(db, sql)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="minidb", description="Base de datos hecha desde cero.")
    parser.add_argument("script", nargs="?", type=Path, help="archivo .sql para ejecutar")
    parser.add_argument(
        "--db", type=Path, help="archivo donde se guarda la base (si no, en memoria)"
    )
    args = parser.parse_args(argv)

    try:
        db = Database(args.db)
    except (DBError, OSError) as e:
        parser.exit(1, f"no se pudo abrir {args.db}: {e}\n")

    with db:
        if args.script is not None:
            run_sql(db, args.script.read_text(encoding="utf-8-sig"))
        else:
            repl(db)


if __name__ == "__main__":
    main()
