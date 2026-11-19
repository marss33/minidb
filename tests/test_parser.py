import pytest

from minidb.errors import SQLSyntaxError
from minidb.nodes import (
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
    Insert,
    IsNull,
    Join,
    Literal,
    OrderBy,
    Rollback,
    Select,
    SelectItem,
    Statement,
    TableRef,
    UnaryOp,
    Update,
)
from minidb.parser import parse


def one(sql: str) -> Statement:
    statements = parse(sql)
    assert len(statements) == 1
    return statements[0]


def where_of(condition: str) -> object:
    stmt = one(f"SELECT * FROM t WHERE {condition}")
    assert isinstance(stmt, Select)
    return stmt.where


def test_create_table() -> None:
    assert one("CREATE TABLE t (id INT, nombre TEXT)") == CreateTable(
        "t", (ColumnDef("id", "INT"), ColumnDef("nombre", "TEXT"))
    )


def test_drop_table() -> None:
    assert one("DROP TABLE t") == DropTable("t")


def test_insert_varias_filas() -> None:
    assert one("INSERT INTO t VALUES (1, 'a'), (2, NULL)") == Insert(
        "t",
        None,
        ((Literal(1), Literal("a")), (Literal(2), Literal(None))),
    )


def test_insert_con_columnas() -> None:
    stmt = one("INSERT INTO t (nombre, id) VALUES ('a', 1)")
    assert isinstance(stmt, Insert)
    assert stmt.columns == ("nombre", "id")


def test_select_completo() -> None:
    assert one("SELECT a, b FROM t WHERE a > 1 ORDER BY b DESC, a LIMIT 5") == Select(
        table=TableRef("t"),
        columns=(SelectItem(Column("a")), SelectItem(Column("b"))),
        where=BinaryOp(">", Column("a"), Literal(1)),
        order_by=(OrderBy(Column("b"), descending=True), OrderBy(Column("a"))),
        limit=5,
    )


def test_join_con_alias_y_columnas_calificadas() -> None:
    sql = """
        SELECT a.nombre, m.nombre FROM alumnos a
        JOIN inscripciones AS i ON i.alumno = a.id
        LEFT JOIN materias m ON m.id = i.materia
        WHERE a.id > 1 ORDER BY m.nombre
    """
    assert one(sql) == Select(
        table=TableRef("alumnos", "a"),
        columns=(SelectItem(Column("nombre", "a")), SelectItem(Column("nombre", "m"))),
        joins=(
            Join(
                TableRef("inscripciones", "i"),
                BinaryOp("=", Column("alumno", "i"), Column("id", "a")),
            ),
            Join(
                TableRef("materias", "m"),
                BinaryOp("=", Column("id", "m"), Column("materia", "i")),
                left=True,
            ),
        ),
        where=BinaryOp(">", Column("id", "a"), Literal(1)),
        order_by=(OrderBy(Column("nombre", "m")),),
    )


def test_inner_join_y_left_outer_join() -> None:
    stmt = one("SELECT * FROM a INNER JOIN b ON 1 = 1 LEFT OUTER JOIN c ON 1 = 1")
    assert isinstance(stmt, Select)
    assert [(j.table.name, j.left) for j in stmt.joins] == [("b", False), ("c", True)]


def test_indices() -> None:
    assert one("CREATE INDEX idx_nota ON alumnos (nota)") == CreateIndex(
        "idx_nota", "alumnos", "nota"
    )
    assert one("DROP INDEX idx_nota") == DropIndex("idx_nota")


def test_explain() -> None:
    assert one("EXPLAIN DELETE FROM t") == Explain(Delete("t"))
    with pytest.raises(SQLSyntaxError, match="EXPLAIN solo"):
        one("EXPLAIN DROP TABLE t")


def test_select_asterisco() -> None:
    stmt = one("SELECT * FROM t")
    assert isinstance(stmt, Select)
    assert stmt.columns is None


def test_update() -> None:
    assert one("UPDATE t SET a = a + 1, b = 'x' WHERE id = 3") == Update(
        "t",
        (("a", BinaryOp("+", Column("a"), Literal(1))), ("b", Literal("x"))),
        BinaryOp("=", Column("id"), Literal(3)),
    )


def test_delete_sin_where() -> None:
    assert one("DELETE FROM t") == Delete("t", None)


def test_and_tiene_mas_precedencia_que_or() -> None:
    assert where_of("a = 1 OR b = 2 AND c = 3") == BinaryOp(
        "OR",
        BinaryOp("=", Column("a"), Literal(1)),
        BinaryOp(
            "AND",
            BinaryOp("=", Column("b"), Literal(2)),
            BinaryOp("=", Column("c"), Literal(3)),
        ),
    )


def test_parentesis_cambian_la_precedencia() -> None:
    expr = where_of("(a = 1 OR b = 2) AND c = 3")
    assert isinstance(expr, BinaryOp)
    assert expr.op == "AND"


def test_multiplicacion_antes_que_suma() -> None:
    assert where_of("a + 2 * 3 = 7") == BinaryOp(
        "=",
        BinaryOp("+", Column("a"), BinaryOp("*", Literal(2), Literal(3))),
        Literal(7),
    )


def test_resta_asocia_a_izquierda() -> None:
    assert where_of("a - 1 - 2 = 0") == BinaryOp(
        "=",
        BinaryOp("-", BinaryOp("-", Column("a"), Literal(1)), Literal(2)),
        Literal(0),
    )


def test_numero_negativo() -> None:
    assert where_of("a = -5") == BinaryOp("=", Column("a"), Literal(-5))
    assert where_of("-a = 5") == BinaryOp("=", UnaryOp("-", Column("a")), Literal(5))


def test_not_e_is_null() -> None:
    assert where_of("NOT a IS NULL") == UnaryOp("NOT", IsNull(Column("a")))
    assert where_of("a IS NOT NULL") == IsNull(Column("a"), negated=True)


def test_distinto_se_normaliza() -> None:
    assert where_of("a <> 1") == where_of("a != 1")


def test_varias_sentencias() -> None:
    statements = parse("CREATE TABLE t (a INT); INSERT INTO t VALUES (1);; SELECT * FROM t;")
    assert [type(s) for s in statements] == [CreateTable, Insert, Select]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT FROM t", "se esperaba un valor o una columna"),
        ("SELECT * t", "se esperaba FROM"),
        ("SELECT * FROM t WHERE", "se esperaba un valor"),
        ("CREATE TABLE t (a FLOAT)", "se esperaba un tipo"),
        ("INSERT INTO t VALUES (1", r"se esperaba \)"),
        ("SELECT * FROM t SELECT", "se esperaba ;"),
        ("BORRAR TODO", "se esperaba SELECT"),
    ],
)
def test_errores_de_sintaxis(sql: str, message: str) -> None:
    with pytest.raises(SQLSyntaxError, match=message):
        parse(sql)


def test_transacciones() -> None:
    assert parse("BEGIN; COMMIT; BEGIN TRANSACTION; ROLLBACK TRANSACTION") == [
        Begin(),
        Commit(),
        Begin(),
        Rollback(),
    ]


def test_restricciones_de_columnas() -> None:
    stmt = one("CREATE TABLE t (id INT PRIMARY KEY, email TEXT UNIQUE NOT NULL, x INT)")
    assert stmt == CreateTable(
        "t",
        (
            ColumnDef("id", "INT", primary_key=True),
            ColumnDef("email", "TEXT", unique=True, not_null=True),
            ColumnDef("x", "INT"),
        ),
    )
    assert one("CREATE UNIQUE INDEX i ON t (email)") == CreateIndex("i", "t", "email", unique=True)


def test_group_by_having_y_funciones() -> None:
    stmt = one("""
        SELECT alumno, COUNT(*) AS n, SUM(nota + 1) FROM notas
        GROUP BY alumno HAVING COUNT(*) > 1 ORDER BY n DESC
    """)
    assert stmt == Select(
        table=TableRef("notas"),
        columns=(
            SelectItem(Column("alumno")),
            SelectItem(Aggregate("COUNT", None), "n"),
            SelectItem(Aggregate("SUM", BinaryOp("+", Column("nota"), Literal(1)))),
        ),
        group_by=(Column("alumno"),),
        having=BinaryOp(">", Aggregate("COUNT", None), Literal(1)),
        order_by=(OrderBy(Column("n"), descending=True),),
    )
