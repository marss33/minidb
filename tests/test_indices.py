import random

import pytest

from minidb import Database, ExecutionError


@pytest.fixture
def db(empty: Database) -> Database:
    empty.execute("CREATE TABLE alumnos (id INT, nombre TEXT, nota INT)")
    rows = ", ".join(f"({i}, 'alumno {i}', {i % 10})" for i in range(1, 201))
    empty.execute(f"INSERT INTO alumnos VALUES {rows}")
    return empty


def plan(db: Database, sql: str) -> list[str]:
    return [str(line) for (line,) in db.query(f"EXPLAIN {sql}")]


def test_sin_indice_recorre_la_tabla(db: Database) -> None:
    assert plan(db, "SELECT * FROM alumnos WHERE id = 5") == ["SCAN alumnos"]


def test_con_indice_busca(db: Database) -> None:
    db.execute("CREATE INDEX alumnos_id ON alumnos (id)")
    assert plan(db, "SELECT * FROM alumnos WHERE id = 5") == [
        "SEARCH alumnos USING INDEX alumnos_id (id = 5)"
    ]
    assert db.query("SELECT nombre FROM alumnos WHERE id = 5") == [("alumno 5",)]


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ("id > 190", "id > 190"),
        ("190 < id", "id > 190"),
        ("id >= 10 AND id < 20", "id >= 10 AND id < 20"),
        ("id > 3 AND id >= 5 AND id <= 9 AND id < 100", "id >= 5 AND id <= 9"),
        ("id > 50 AND nota = 3", "id > 50"),
        ("id >= 5 AND id <= 5", "id = 5"),
    ],
)
def test_rangos(db: Database, where: str, expected: str) -> None:
    db.execute("CREATE INDEX alumnos_id ON alumnos (id)")
    assert plan(db, f"SELECT * FROM alumnos WHERE {where}") == [
        f"SEARCH alumnos USING INDEX alumnos_id ({expected})"
    ]


def test_prefiere_una_igualdad(db: Database) -> None:
    db.execute(
        "CREATE INDEX alumnos_id ON alumnos (id); CREATE INDEX alumnos_nota ON alumnos (nota)"
    )
    assert plan(db, "SELECT * FROM alumnos WHERE id > 10 AND nota = 3") == [
        "SEARCH alumnos USING INDEX alumnos_nota (nota = 3)"
    ]


@pytest.mark.parametrize(
    "where",
    [
        "id = 5 OR id = 6",  # con OR haria falta buscar dos veces
        "id + 0 = 5",  # el indice es sobre id, no sobre id + 0
        "id = nota",  # no es un valor fijo
        "id != 5",
        "id IS NULL",
    ],
)
def test_casos_que_no_usan_el_indice(db: Database, where: str) -> None:
    db.execute("CREATE INDEX alumnos_id ON alumnos (id)")
    assert plan(db, f"SELECT * FROM alumnos WHERE {where}") == ["SCAN alumnos"]


def test_comparar_con_otro_tipo_sigue_dando_error(db: Database) -> None:
    db.execute("CREATE INDEX alumnos_nombre ON alumnos (nombre)")
    with pytest.raises(ExecutionError, match="no se puede comparar TEXT con INT"):
        db.query("SELECT * FROM alumnos WHERE nombre = 3")


def test_indice_sobre_texto(db: Database) -> None:
    db.execute("CREATE INDEX alumnos_nombre ON alumnos (nombre)")
    assert plan(db, "SELECT id FROM alumnos WHERE nombre = 'alumno 7'") == [
        "SEARCH alumnos USING INDEX alumnos_nombre (nombre = 'alumno 7')"
    ]
    assert db.query("SELECT id FROM alumnos WHERE nombre = 'alumno 7'") == [(7,)]


def test_los_null_no_aparecen_en_las_busquedas(db: Database) -> None:
    db.execute("CREATE INDEX alumnos_nota ON alumnos (nota)")
    db.execute("INSERT INTO alumnos VALUES (500, 'sin nota', NULL)")
    assert (500,) not in db.query("SELECT id FROM alumnos WHERE nota < 100")
    assert len(db.query("SELECT id FROM alumnos WHERE nota < 100")) == 200


def test_update_y_delete_usan_y_mantienen_el_indice(db: Database) -> None:
    db.execute("CREATE INDEX alumnos_nota ON alumnos (nota)")
    assert plan(db, "UPDATE alumnos SET nota = 10 WHERE nota = 9") == [
        "SEARCH alumnos USING INDEX alumnos_nota (nota = 9)"
    ]
    assert plan(db, "DELETE FROM alumnos WHERE nota < 2") == [
        "SEARCH alumnos USING INDEX alumnos_nota (nota < 2)"
    ]

    (result,) = db.execute("UPDATE alumnos SET nota = 10 WHERE nota = 9")
    assert result.message == "20 filas actualizadas"
    assert db.query("SELECT id FROM alumnos WHERE nota = 9") == []
    assert len(db.query("SELECT id FROM alumnos WHERE nota = 10")) == 20

    (result,) = db.execute("DELETE FROM alumnos WHERE nota < 2")
    assert result.message == "40 filas borradas"
    assert db.query("SELECT id FROM alumnos WHERE nota = 0") == []
    assert len(db.query("SELECT id FROM alumnos WHERE nota >= 0")) == 160


def test_update_que_cambia_la_columna_que_se_busca(db: Database) -> None:
    db.execute("CREATE INDEX alumnos_id ON alumnos (id)")
    db.execute("UPDATE alumnos SET id = id + 1000 WHERE id > 100")
    assert db.query("SELECT id FROM alumnos WHERE id > 100 AND id < 1000") == []
    assert len(db.query("SELECT id FROM alumnos WHERE id > 1000")) == 100


def test_el_indice_no_cambia_los_resultados(empty: Database) -> None:
    """Las mismas consultas, con y sin indices, sobre datos al azar."""
    db = empty
    rng = random.Random(5)
    db.execute("CREATE TABLE t (a INT, b TEXT, c INT)")
    rows = []
    for _ in range(400):
        a = rng.choice([None, *range(-20, 20)])
        b = rng.choice(["NULL", "'x'", "'y'", "'zz'", "''"])
        rows.append(f"({'NULL' if a is None else a}, {b}, {rng.randint(0, 5)})")
    db.execute(f"INSERT INTO t VALUES {', '.join(rows)}")

    queries = [
        "SELECT * FROM t WHERE a = 3",
        "SELECT * FROM t WHERE a > -5 AND a <= 7",
        "SELECT * FROM t WHERE a < 0 AND c = 2",
        "SELECT b, a FROM t WHERE b = 'zz' ORDER BY a DESC LIMIT 7",
        "SELECT * FROM t WHERE b >= 'y' AND a >= 10",
        "SELECT * FROM t WHERE 4 >= a AND a > 4",
    ]
    before = [db.query(q) for q in queries]
    db.execute("CREATE INDEX t_a ON t (a); CREATE INDEX t_b ON t (b)")
    assert all(line.startswith("SEARCH") for q in queries for line in plan(db, q))
    assert [db.query(q) for q in queries] == before


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("CREATE INDEX i ON nada (a)", "no existe la tabla nada"),
        ("CREATE INDEX i ON alumnos (edad)", "no tiene la columna edad"),
        ("CREATE INDEX alumnos ON alumnos (id)", "ya existe una tabla llamada alumnos"),
        ("DROP INDEX nada", "no existe el indice nada"),
        ("EXPLAIN SELECT edad FROM alumnos", "no tiene la columna edad"),
    ],
)
def test_errores(db: Database, sql: str, message: str) -> None:
    with pytest.raises(ExecutionError, match=message):
        db.execute(sql)


def test_nombres_repetidos(db: Database) -> None:
    db.execute("CREATE INDEX i ON alumnos (id)")
    with pytest.raises(ExecutionError, match="ya existe"):
        db.execute("CREATE INDEX i ON alumnos (nota)")
    with pytest.raises(ExecutionError, match="ya existe un indice llamado i"):
        db.execute("CREATE TABLE i (a INT)")


def test_drop_index_y_drop_table(db: Database) -> None:
    db.execute("CREATE INDEX i ON alumnos (id)")
    db.execute("DROP INDEX i")
    assert db.indexes == {}
    assert plan(db, "SELECT * FROM alumnos WHERE id = 1") == ["SCAN alumnos"]

    db.execute("CREATE INDEX i ON alumnos (id)")
    db.execute("DROP TABLE alumnos")
    assert db.indexes == {}
    # el nombre queda libre
    db.execute("CREATE TABLE alumnos (id INT); CREATE INDEX i ON alumnos (id)")
    db.execute("INSERT INTO alumnos VALUES (1)")
    assert db.query("SELECT * FROM alumnos WHERE id = 1") == [(1,)]
