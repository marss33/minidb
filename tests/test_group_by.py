import pytest

from minidb import Database, ExecutionError, SQLSyntaxError


@pytest.fixture
def db(empty: Database) -> Database:
    empty.execute("""
        CREATE TABLE notas (alumno TEXT, materia TEXT, nota INT);
        INSERT INTO notas VALUES
            ('Ana', 'Algebra', 9), ('Ana', 'Fisica', 7), ('Ana', 'Historia', NULL),
            ('Bruno', 'Algebra', 4), ('Bruno', 'Fisica', 6),
            ('Carla', 'Algebra', 10),
            (NULL, 'Fisica', 8);
    """)
    return empty


def test_funciones_sobre_toda_la_tabla(db: Database) -> None:
    (result,) = db.execute(
        "SELECT COUNT(*), COUNT(nota), SUM(nota), MIN(nota), MAX(nota), AVG(nota) FROM notas"
    )
    assert result.columns == (
        "count(*)", "count(nota)", "sum(nota)", "min(nota)", "max(nota)", "avg(nota)"
    )  # fmt: skip
    # el promedio de enteros es entero: 44 / 6 = 7,33 -> 7
    assert result.rows == [(7, 6, 44, 4, 10, 7)]


def test_tabla_vacia(db: Database) -> None:
    db.execute("DELETE FROM notas")
    assert db.query("SELECT COUNT(*), SUM(nota), MAX(alumno) FROM notas") == [(0, None, None)]
    assert db.query("SELECT alumno, COUNT(*) FROM notas GROUP BY alumno") == []


def test_group_by(db: Database) -> None:
    rows = db.query("""
        SELECT alumno, COUNT(*) AS materias, SUM(nota) AS total, MAX(materia)
        FROM notas GROUP BY alumno ORDER BY alumno
    """)
    # los NULL quedan juntos en un grupo y van primero al ordenar
    assert rows == [
        (None, 1, 8, "Fisica"),
        ("Ana", 3, 16, "Historia"),
        ("Bruno", 2, 10, "Fisica"),
        ("Carla", 1, 10, "Algebra"),
    ]


def test_having_y_order_by_por_alias(db: Database) -> None:
    rows = db.query("""
        SELECT alumno, AVG(nota) AS promedio FROM notas
        WHERE alumno IS NOT NULL
        GROUP BY alumno
        HAVING COUNT(nota) >= 2
        ORDER BY promedio DESC
    """)
    assert rows == [("Ana", 8), ("Bruno", 5)]


def test_order_by_por_una_funcion(db: Database) -> None:
    rows = db.query("SELECT materia FROM notas GROUP BY materia ORDER BY COUNT(*) DESC, materia")
    assert rows == [("Algebra",), ("Fisica",), ("Historia",)]


def test_group_by_varias_columnas_y_join(db: Database) -> None:
    db.execute("""
        CREATE TABLE materias (nombre TEXT PRIMARY KEY, anio INT);
        INSERT INTO materias VALUES ('Algebra', 1), ('Fisica', 1), ('Historia', 2);
    """)
    rows = db.query("""
        SELECT m.anio, n.alumno, COUNT(*) FROM notas n
        JOIN materias m ON m.nombre = n.materia
        WHERE n.alumno IS NOT NULL
        GROUP BY m.anio, n.alumno
        ORDER BY m.anio, n.alumno
    """)
    assert rows == [(1, "Ana", 2), (1, "Bruno", 2), (1, "Carla", 1), (2, "Ana", 1)]


def test_expresiones_y_alias_sin_agrupar(db: Database) -> None:
    (result,) = db.execute("""
        SELECT alumno, nota * 10 AS sobre_cien, nota > 6 FROM notas
        WHERE alumno = 'Bruno' ORDER BY nota * -1
    """)
    assert result.columns == ("alumno", "sobre_cien", "nota > 6")
    assert result.rows == [("Bruno", 60, False), ("Bruno", 40, False)]


def test_expresiones_con_funciones(db: Database) -> None:
    rows = db.query("SELECT MAX(nota) - MIN(nota), COUNT(*) * 2 + 1 FROM notas")
    assert rows == [(6, 15)]


def test_explain_muestra_el_group_by(db: Database) -> None:
    assert db.query("EXPLAIN SELECT alumno, COUNT(*) FROM notas GROUP BY alumno") == [
        ("SCAN notas",),
        ("GROUP BY alumno",),
    ]
    assert db.query("EXPLAIN SELECT COUNT(*) FROM notas") == [("SCAN notas",), ("GROUP BY (todo)",)]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT alumno, COUNT(*) FROM notas", "alumno tiene que estar en el GROUP BY"),
        ("SELECT materia FROM notas GROUP BY alumno", "materia tiene que estar en el GROUP BY"),
        ("SELECT * FROM notas GROUP BY alumno", "SELECT \\* con GROUP BY"),
        ("SELECT alumno FROM notas WHERE COUNT(*) > 1", "COUNT en el WHERE"),
        ("SELECT SUM(COUNT(*)) FROM notas", "dentro de otra"),
        ("SELECT SUM(alumno) FROM notas", "'SUM' necesita numeros"),
        ("SELECT MAX(nota > 5) FROM notas", "MAX necesita valores del mismo tipo"),
        ("UPDATE notas SET nota = COUNT(*)", "COUNT en el SET"),
        ("INSERT INTO notas VALUES ('x', 'y', COUNT(*))", "funciones como COUNT en VALUES"),
    ],
)
def test_errores(db: Database, sql: str, message: str) -> None:
    with pytest.raises(ExecutionError, match=message):
        db.execute(sql)


def test_funcion_desconocida(db: Database) -> None:
    with pytest.raises(SQLSyntaxError, match="no existe la funcion upper"):
        db.execute("SELECT upper(alumno) FROM notas")


def test_una_columna_que_se_llama_count(empty: Database) -> None:
    empty.execute("CREATE TABLE t (count INT); INSERT INTO t VALUES (3), (4)")
    assert empty.query("SELECT count, COUNT(count) FROM t GROUP BY count") == [(3, 1), (4, 1)]
