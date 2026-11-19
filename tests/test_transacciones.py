from pathlib import Path

import pytest

from minidb import Database, ExecutionError


@pytest.fixture
def db(empty: Database) -> Database:
    empty.execute("CREATE TABLE cuentas (id INT PRIMARY KEY, saldo INT NOT NULL)")
    empty.execute("INSERT INTO cuentas VALUES (1, 100), (2, 50)")
    return empty


def saldos(db: Database) -> list[tuple[object, ...]]:
    return db.query("SELECT id, saldo FROM cuentas ORDER BY id")


def test_commit(db: Database) -> None:
    results = db.execute("""
        BEGIN;
        UPDATE cuentas SET saldo = saldo - 30 WHERE id = 1;
        UPDATE cuentas SET saldo = saldo + 30 WHERE id = 2;
        COMMIT;
    """)
    assert [r.message for r in results] == [
        "transaccion empezada",
        "1 fila actualizada",
        "1 fila actualizada",
        "transaccion guardada",
    ]
    assert saldos(db) == [(1, 70), (2, 80)]
    assert not db.in_transaction


def test_rollback_deshace_todo(db: Database) -> None:
    db.execute("BEGIN TRANSACTION")
    assert db.in_transaction
    db.execute("""
        UPDATE cuentas SET saldo = 0;
        INSERT INTO cuentas VALUES (3, 10);
        CREATE TABLE otra (a INT);
        CREATE INDEX cuentas_saldo ON cuentas (saldo);
    """)
    assert saldos(db) == [(1, 0), (2, 0), (3, 10)]
    (result,) = db.execute("ROLLBACK")
    assert result.message == "transaccion deshecha"
    assert saldos(db) == [(1, 100), (2, 50)]
    assert "otra" not in db.tables
    assert "cuentas_saldo" not in db.indexes


def test_rollback_de_un_drop_table(db: Database) -> None:
    db.execute("CREATE INDEX cuentas_saldo ON cuentas (saldo)")
    db.execute("BEGIN; DROP TABLE cuentas")
    assert "cuentas" not in db.tables
    db.execute("ROLLBACK")
    assert saldos(db) == [(1, 100), (2, 50)]
    assert set(db.indexes) == {"cuentas_pkey", "cuentas_saldo"}
    assert db.query("SELECT id FROM cuentas WHERE saldo = 50") == [(2,)]


def test_un_error_deshace_toda_la_transaccion(db: Database) -> None:
    db.execute("BEGIN; UPDATE cuentas SET saldo = 0 WHERE id = 1")
    with pytest.raises(ExecutionError, match="se deshizo toda la transaccion"):
        db.execute("INSERT INTO cuentas VALUES (2, 1)")  # id repetido
    assert not db.in_transaction
    assert saldos(db) == [(1, 100), (2, 50)]


def test_sin_transaccion_cada_sentencia_se_guarda_sola(db: Database) -> None:
    with pytest.raises(ExecutionError):
        db.execute("UPDATE cuentas SET saldo = 0 WHERE id = 1; INSERT INTO cuentas VALUES (1, 1)")
    assert saldos(db) == [(1, 0), (2, 50)]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("BEGIN; BEGIN", "ya hay una transaccion abierta"),
        ("COMMIT", "no hay ninguna transaccion abierta"),
        ("ROLLBACK", "no hay ninguna transaccion abierta"),
    ],
)
def test_errores(db: Database, sql: str, message: str) -> None:
    with pytest.raises(ExecutionError, match=message):
        db.execute(sql)


def test_en_disco_solo_se_guarda_con_commit(tmp_path: Path) -> None:
    path = tmp_path / "datos.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (n INT); INSERT INTO t VALUES (1)")
        db.execute("BEGIN; INSERT INTO t VALUES (2); COMMIT")
        db.execute("BEGIN; INSERT INTO t VALUES (3)")
        assert db.query("SELECT n FROM t") == [(1,), (2,), (3,)]
        # se cierra sin COMMIT

    with Database(path) as db:
        assert db.query("SELECT n FROM t") == [(1,), (2,)]
        assert not db.in_transaction


def test_transaccion_grande_en_disco(tmp_path: Path) -> None:
    """Muchas paginas modificadas antes del commit (todas quedan en memoria hasta ahi)."""
    path = tmp_path / "datos.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (n INT, s TEXT)")
        db.execute("BEGIN")
        for start in range(0, 20_000, 2000):
            values = ", ".join(f"({i}, 'fila numero {i}')" for i in range(start, start + 2000))
            db.execute(f"INSERT INTO t VALUES {values}")
        db.execute("DELETE FROM t WHERE n >= 10000")
        db.execute("COMMIT")

    with Database(path) as db:
        assert len(db.query("SELECT n FROM t")) == 10_000
