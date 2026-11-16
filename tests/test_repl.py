from minidb import Database
from minidb.repl import format_result


def test_formato_de_tabla() -> None:
    db = Database()
    db.execute("CREATE TABLE t (id INT, nombre TEXT); INSERT INTO t VALUES (1, 'Ana'), (10, NULL)")
    (result,) = db.execute("SELECT * FROM t")
    assert format_result(result) == "\n".join(
        [
            "+----+--------+",
            "| id | nombre |",
            "+----+--------+",
            "|  1 | Ana    |",
            "| 10 | NULL   |",
            "+----+--------+",
            "(2 filas)",
        ]
    )


def test_formato_de_mensaje() -> None:
    db = Database()
    (result,) = db.execute("CREATE TABLE t (a INT)")
    assert format_result(result) == "tabla t creada"
