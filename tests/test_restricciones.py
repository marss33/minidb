import re
from pathlib import Path

import pytest

from minidb import Database, ExecutionError


@pytest.fixture
def db(empty: Database) -> Database:
    empty.execute("""
        CREATE TABLE alumnos (
            legajo INT PRIMARY KEY,
            email TEXT UNIQUE,
            nombre TEXT NOT NULL
        );
        INSERT INTO alumnos VALUES (1, 'ana@uni.edu', 'Ana'), (2, NULL, 'Bruno');
    """)
    return empty


def test_el_esquema_guarda_las_restricciones(db: Database) -> None:
    assert db.tables["alumnos"].schema() == (
        "CREATE TABLE alumnos (legajo INT PRIMARY KEY, email TEXT UNIQUE, nombre TEXT NOT NULL)"
    )
    assert sorted(db.indexes) == ["alumnos_email_key", "alumnos_pkey"]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("INSERT INTO alumnos VALUES (1, 'x@uni.edu', 'X')", "legajo = 1 en alumnos (PRIMARY KEY)"),
        ("INSERT INTO alumnos VALUES (3, 'x', 'X'), (3, 'y', 'Y')", "legajo = 3"),
        (
            "INSERT INTO alumnos VALUES (3, 'ana@uni.edu', 'X')",
            "email = 'ana@uni.edu' en alumnos (UNIQUE)",
        ),
        ("INSERT INTO alumnos VALUES (NULL, 'x', 'X')", "legajo no puede ser NULL"),
        ("INSERT INTO alumnos (legajo, email) VALUES (3, 'x')", "nombre no puede ser NULL"),
        ("UPDATE alumnos SET nombre = NULL WHERE legajo = 2", "nombre no puede ser NULL"),
        ("UPDATE alumnos SET legajo = 1 WHERE legajo = 2", "legajo = 1"),
        ("UPDATE alumnos SET email = 'z'", "email = 'z'"),
    ],
)
def test_errores(db: Database, sql: str, message: str) -> None:
    before = db.query("SELECT * FROM alumnos")
    with pytest.raises(ExecutionError, match=re.escape(message)):
        db.execute(sql)
    assert db.query("SELECT * FROM alumnos") == before


def test_unique_permite_varios_null(db: Database) -> None:
    db.execute("INSERT INTO alumnos VALUES (3, NULL, 'Carla'), (4, NULL, 'Diego')")
    assert len(db.query("SELECT * FROM alumnos WHERE email IS NULL")) == 3


def test_update_que_corre_todos_los_valores(db: Database) -> None:
    # 1 -> 2 y 2 -> 3: el 2 "choca" con una fila que tambien cambia, asi que esta bien
    db.execute("UPDATE alumnos SET legajo = legajo + 1")
    assert db.query("SELECT legajo FROM alumnos") == [(2,), (3,)]


def test_update_que_no_cambia_el_valor(db: Database) -> None:
    db.execute("UPDATE alumnos SET email = 'ana@uni.edu', nombre = 'Ana Maria' WHERE legajo = 1")
    assert db.query("SELECT nombre FROM alumnos WHERE legajo = 1") == [("Ana Maria",)]


def test_la_primary_key_tiene_indice(db: Database) -> None:
    assert db.query("EXPLAIN SELECT * FROM alumnos WHERE legajo = 2") == [
        ("SEARCH alumnos USING INDEX alumnos_pkey (legajo = 2)",)
    ]


def test_no_se_puede_borrar_el_indice_de_una_restriccion(db: Database) -> None:
    with pytest.raises(ExecutionError, match="es parte de la definicion de la tabla alumnos"):
        db.execute("DROP INDEX alumnos_pkey")


def test_create_unique_index(db: Database) -> None:
    db.execute("CREATE UNIQUE INDEX alumnos_nombre ON alumnos (nombre)")
    with pytest.raises(ExecutionError, match="nombre = 'Ana'"):
        db.execute("INSERT INTO alumnos VALUES (3, NULL, 'Ana')")
    db.execute("DROP INDEX alumnos_nombre")
    db.execute("INSERT INTO alumnos VALUES (3, NULL, 'Ana')")
    with pytest.raises(ExecutionError, match="nombre = 'Ana'"):
        db.execute("CREATE UNIQUE INDEX alumnos_nombre ON alumnos (nombre)")


def test_muchas_filas_de_una_vez(db: Database) -> None:
    values = ", ".join(f"({i}, 'alumno{i}@uni.edu', 'A')" for i in range(10, 500))
    db.execute(f"INSERT INTO alumnos VALUES {values}")
    with pytest.raises(ExecutionError, match="legajo = 10 "):
        db.execute(f"INSERT INTO alumnos VALUES {values.replace('@', '+otro@')}")
    with pytest.raises(ExecutionError, match="email = 'alumno10@uni.edu'"):
        values = ", ".join(f"({i}, 'alumno{i - 1000}@uni.edu', 'A')" for i in range(1010, 1100))
        db.execute(f"INSERT INTO alumnos VALUES {values}")


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("CREATE TABLE t (a INT PRIMARY KEY, b INT PRIMARY KEY)", "una sola PRIMARY KEY"),
        ("CREATE TABLE alumnos_pkey (a INT)", "ya existe un indice llamado alumnos_pkey"),
    ],
)
def test_errores_al_crear(db: Database, sql: str, message: str) -> None:
    with pytest.raises(ExecutionError, match=message):
        db.execute(sql)


def test_las_restricciones_sobreviven_a_reabrir(tmp_path: Path) -> None:
    path = tmp_path / "datos.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, nombre TEXT NOT NULL)")
        db.execute("INSERT INTO t VALUES (1, 'a')")
    with Database(path) as db:
        with pytest.raises(ExecutionError, match="PRIMARY KEY"):
            db.execute("INSERT INTO t VALUES (1, 'b')")
        with pytest.raises(ExecutionError, match="no puede ser NULL"):
            db.execute("INSERT INTO t VALUES (2, NULL)")
