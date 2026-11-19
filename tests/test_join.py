import pytest

from minidb import Database, ExecutionError


@pytest.fixture
def db(empty: Database) -> Database:
    empty.execute("""
        CREATE TABLE alumnos (id INT, nombre TEXT);
        CREATE TABLE materias (id INT, nombre TEXT);
        CREATE TABLE notas (alumno INT, materia INT, nota INT);

        INSERT INTO alumnos VALUES (1, 'Ana'), (2, 'Bruno'), (3, 'Carla');
        INSERT INTO materias VALUES (10, 'Algebra'), (20, 'Fisica'), (30, 'Historia');
        INSERT INTO notas VALUES
            (1, 10, 9), (1, 20, 7),
            (2, 10, 4),
            (9, 30, 8);  -- un alumno que no existe
    """)
    return empty


def test_join_simple(db: Database) -> None:
    rows = db.query("""
        SELECT nombre, nota FROM alumnos JOIN notas ON alumno = id
        ORDER BY nombre, nota
    """)
    assert rows == [("Ana", 7), ("Ana", 9), ("Bruno", 4)]


def test_tres_tablas_con_alias(db: Database) -> None:
    rows = db.query("""
        SELECT a.nombre, m.nombre, n.nota
        FROM notas n
        JOIN alumnos a ON a.id = n.alumno
        JOIN materias AS m ON m.id = n.materia
        WHERE n.nota >= 7
        ORDER BY n.nota DESC
    """)
    assert rows == [("Ana", "Algebra", 9), ("Ana", "Fisica", 7)]


def test_nombres_de_las_columnas(db: Database) -> None:
    (result,) = db.execute("SELECT * FROM alumnos a JOIN notas n ON n.alumno = a.id LIMIT 1")
    assert result.columns == ("id", "nombre", "alumno", "materia", "nota")
    assert result.rows == [(1, "Ana", 1, 10, 9)]


def test_left_join_completa_con_null(db: Database) -> None:
    rows = db.query("""
        SELECT a.nombre, n.nota FROM alumnos a
        LEFT JOIN notas n ON n.alumno = a.id
        ORDER BY a.id, n.nota
    """)
    assert rows == [("Ana", 7), ("Ana", 9), ("Bruno", 4), ("Carla", None)]


def test_left_join_para_buscar_lo_que_falta(db: Database) -> None:
    rows = db.query("""
        SELECT m.nombre FROM materias m
        LEFT JOIN notas n ON n.materia = m.id AND n.alumno = 1
        WHERE n.alumno IS NULL
    """)
    assert rows == [("Historia",)]


def test_join_de_una_tabla_consigo_misma(db: Database) -> None:
    rows = db.query("""
        SELECT x.nombre, y.nombre FROM alumnos x JOIN alumnos y ON y.id = x.id + 1
    """)
    assert rows == [("Ana", "Bruno"), ("Bruno", "Carla")]


def test_join_con_condicion_que_no_es_igualdad(db: Database) -> None:
    rows = db.query("SELECT a.id, m.id FROM alumnos a JOIN materias m ON m.id < a.id * 10")
    assert rows == [(2, 10), (3, 10), (3, 20)]


def test_join_usa_el_indice_de_la_tabla_de_la_derecha(db: Database) -> None:
    sql = """
        SELECT a.nombre, n.nota FROM alumnos a JOIN notas n ON n.alumno = a.id
        WHERE a.id = 1 ORDER BY n.nota
    """
    before = db.query(sql)
    assert db.query(f"EXPLAIN {sql}") == [("SCAN a",), ("SCAN n",)]

    db.execute(
        "CREATE INDEX notas_alumno ON notas (alumno); CREATE INDEX alumnos_id ON alumnos (id)"
    )
    assert db.query(f"EXPLAIN {sql}") == [
        ("SEARCH a USING INDEX alumnos_id (id = 1)",),
        ("SEARCH n USING INDEX notas_alumno (alumno = ?)",),
    ]
    assert db.query(sql) == before == [("Ana", 7), ("Ana", 9)]


def test_left_join_con_indice(db: Database) -> None:
    db.execute("CREATE INDEX notas_alumno ON notas (alumno)")
    rows = db.query("SELECT a.nombre, n.nota FROM alumnos a LEFT JOIN notas n ON n.alumno = a.id")
    assert ("Carla", None) in rows
    assert len(rows) == 4


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT nombre FROM alumnos JOIN materias ON 1 = 1", "nombre es ambigua"),
        ("SELECT x.id FROM alumnos JOIN notas ON alumno = id", "no hay ninguna tabla x"),
        ("SELECT * FROM alumnos JOIN notas ON alumno = edad", "no existe la columna edad"),
        ("SELECT * FROM alumnos JOIN alumnos ON 1 = 1", "aparece dos veces"),
        # el ON no puede ver tablas que todavia no se unieron
        (
            "SELECT * FROM alumnos a JOIN notas n ON n.materia = m.id JOIN materias m ON 1 = 1",
            "no hay ninguna tabla m",
        ),
        ("SELECT * FROM alumnos JOIN notas ON alumno", "se esperaba una condicion"),
    ],
)
def test_errores(db: Database, sql: str, message: str) -> None:
    with pytest.raises(ExecutionError, match=message):
        db.query(sql)


def test_las_columnas_calificadas_tambien_andan_sin_join(db: Database) -> None:
    assert db.query("SELECT alumnos.nombre FROM alumnos WHERE alumnos.id = 2") == [("Bruno",)]
    db.execute("UPDATE alumnos SET nombre = 'Bruna' WHERE alumnos.id = 2")
    assert db.query("SELECT a.nombre FROM alumnos a WHERE a.id = 2") == [("Bruna",)]
