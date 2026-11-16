import pytest

from minidb import Database, ExecutionError


@pytest.fixture
def db() -> Database:
    db = Database()
    db.execute("""
        CREATE TABLE alumnos (id INT, nombre TEXT, nota INT);
        INSERT INTO alumnos VALUES
            (1, 'Ana', 9),
            (2, 'Bruno', 6),
            (3, 'Carla', NULL),
            (4, 'Diego', 4);
    """)
    return db


def test_select_todo(db: Database) -> None:
    assert db.query("SELECT * FROM alumnos") == [
        (1, "Ana", 9),
        (2, "Bruno", 6),
        (3, "Carla", None),
        (4, "Diego", 4),
    ]


def test_select_columnas_en_otro_orden(db: Database) -> None:
    assert db.query("SELECT nota, id FROM alumnos WHERE id = 1") == [(9, 1)]


def test_nombres_de_columnas_en_el_resultado(db: Database) -> None:
    (result,) = db.execute("SELECT nombre, id FROM alumnos")
    assert result.columns == ("nombre", "id")


def test_where_con_and_y_or(db: Database) -> None:
    rows = db.query("SELECT id FROM alumnos WHERE nota > 5 AND nota < 9 OR id = 4")
    assert rows == [(2,), (4,)]


def test_null_no_pasa_ninguna_comparacion(db: Database) -> None:
    # Carla tiene nota NULL: no es > 5 pero tampoco es <= 5
    assert (3,) not in db.query("SELECT id FROM alumnos WHERE nota > 5")
    assert (3,) not in db.query("SELECT id FROM alumnos WHERE nota <= 5")
    assert (3,) not in db.query("SELECT id FROM alumnos WHERE NOT nota > 5")


def test_is_null(db: Database) -> None:
    assert db.query("SELECT id FROM alumnos WHERE nota IS NULL") == [(3,)]
    assert len(db.query("SELECT id FROM alumnos WHERE nota IS NOT NULL")) == 3


def test_logica_de_tres_valores(db: Database) -> None:
    # NULL OR TRUE es TRUE, NULL AND FALSE es FALSE
    assert db.query("SELECT id FROM alumnos WHERE id = 3 AND (nota > 5 OR 1 = 1)") == [(3,)]
    assert db.query("SELECT id FROM alumnos WHERE NOT (nota > 5 AND 1 = 2) AND id = 3") == [(3,)]


def test_order_by_y_limit(db: Database) -> None:
    assert db.query("SELECT nombre FROM alumnos ORDER BY nota DESC LIMIT 2") == [
        ("Ana",),
        ("Bruno",),
    ]


def test_order_by_pone_null_primero(db: Database) -> None:
    assert db.query("SELECT id FROM alumnos ORDER BY nota") == [(3,), (4,), (2,), (1,)]


def test_order_by_varias_columnas() -> None:
    db = Database()
    db.execute("""
        CREATE TABLE t (a INT, b TEXT);
        INSERT INTO t VALUES (1, 'z'), (2, 'x'), (1, 'y'), (2, 'w');
    """)
    assert db.query("SELECT a, b FROM t ORDER BY a DESC, b") == [
        (2, "w"),
        (2, "x"),
        (1, "y"),
        (1, "z"),
    ]


def test_insert_con_columnas_deja_el_resto_en_null(db: Database) -> None:
    db.execute("INSERT INTO alumnos (nombre, id) VALUES ('Eva', 5)")
    assert db.query("SELECT * FROM alumnos WHERE id = 5") == [(5, "Eva", None)]


def test_insert_acepta_expresiones() -> None:
    db = Database()
    db.execute("CREATE TABLE t (a INT)")
    db.execute("INSERT INTO t VALUES (2 * 3 + 1), (-4)")
    assert db.query("SELECT a FROM t") == [(7,), (-4,)]


def test_update(db: Database) -> None:
    (result,) = db.execute("UPDATE alumnos SET nota = nota + 1 WHERE nota < 7")
    assert result.message == "2 filas actualizadas"
    assert db.query("SELECT nota FROM alumnos") == [(9,), (7,), (None,), (5,)]


def test_update_usa_los_valores_viejos() -> None:
    db = Database()
    db.execute("CREATE TABLE t (a INT, b INT); INSERT INTO t VALUES (1, 2)")
    db.execute("UPDATE t SET a = b, b = a")
    assert db.query("SELECT * FROM t") == [(2, 1)]


def test_delete(db: Database) -> None:
    (result,) = db.execute("DELETE FROM alumnos WHERE nota < 7")
    assert result.message == "2 filas borradas"
    assert db.query("SELECT id FROM alumnos") == [(1,), (3,)]


def test_delete_sin_where_borra_todo(db: Database) -> None:
    db.execute("DELETE FROM alumnos")
    assert db.query("SELECT * FROM alumnos") == []


def test_drop_table(db: Database) -> None:
    db.execute("DROP TABLE alumnos")
    with pytest.raises(ExecutionError, match="no existe la tabla"):
        db.query("SELECT * FROM alumnos")


def test_division_entera_trunca_hacia_cero() -> None:
    db = Database()
    db.execute("CREATE TABLE t (a INT); INSERT INTO t VALUES (7), (-7)")
    assert db.query("SELECT a FROM t WHERE a / 2 = 3 OR a / 2 = -3") == [(7,), (-7,)]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT * FROM profes", "no existe la tabla profes"),
        ("SELECT edad FROM alumnos", "no tiene la columna edad"),
        ("SELECT * FROM alumnos WHERE edad > 3", "no tiene la columna edad"),
        ("SELECT * FROM alumnos ORDER BY edad", "no tiene la columna edad"),
        ("CREATE TABLE alumnos (a INT)", "ya existe"),
        ("CREATE TABLE x (a INT, a TEXT)", "mismo nombre"),
        ("INSERT INTO alumnos VALUES (1, 'x')", "se esperaban 3 valores"),
        ("INSERT INTO alumnos VALUES ('uno', 'x', 1)", "es INT y se le paso TEXT"),
        ("INSERT INTO alumnos VALUES (1 = 1, 'x', 1)", "es INT y se le paso BOOL"),
        ("INSERT INTO alumnos VALUES (id, 'x', 1)", "no se pueden usar columnas"),
        ("UPDATE alumnos SET nombre = 5", "es TEXT y se le paso INT"),
        ("SELECT * FROM alumnos WHERE nombre > 3", "no se puede comparar TEXT con INT"),
        ("SELECT * FROM alumnos WHERE nombre + 1 = 2", "necesita numeros"),
        ("SELECT * FROM alumnos WHERE nota", "se esperaba una condicion"),
        ("SELECT * FROM alumnos WHERE nota / 0 = 1", "division por cero"),
    ],
)
def test_errores(db: Database, sql: str, message: str) -> None:
    with pytest.raises(ExecutionError, match=message):
        db.execute(sql)


def test_insert_que_falla_no_guarda_nada(db: Database) -> None:
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO alumnos VALUES (5, 'Eva', 8), (6, 'Fede', 'diez')")
    assert len(db.query("SELECT * FROM alumnos")) == 4


def test_update_que_falla_no_cambia_nada() -> None:
    db = Database()
    db.execute("CREATE TABLE t (a INT); INSERT INTO t VALUES (2), (0)")
    with pytest.raises(ExecutionError, match="division por cero"):
        db.execute("UPDATE t SET a = 10 / a")
    assert db.query("SELECT a FROM t") == [(2,), (0,)]
