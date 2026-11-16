import pytest

from minidb.errors import SQLSyntaxError
from minidb.nodes import (
    BinaryOp,
    Column,
    ColumnDef,
    CreateTable,
    Delete,
    DropTable,
    Insert,
    IsNull,
    Literal,
    OrderBy,
    Select,
    Statement,
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
        table="t",
        columns=("a", "b"),
        where=BinaryOp(">", Column("a"), Literal(1)),
        order_by=(OrderBy("b", descending=True), OrderBy("a")),
        limit=5,
    )


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
        ("SELECT FROM t", "se esperaba un nombre"),
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
