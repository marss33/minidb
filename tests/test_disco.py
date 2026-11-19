"""Cosas que solo tienen sentido con la base guardada en un archivo."""

from pathlib import Path

import pytest

from minidb import Database, ExecutionError
from minidb.repl import main
from minidb.storage import DiskStorage


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "datos.db"


def test_los_datos_sobreviven_a_cerrar_y_abrir(path: Path) -> None:
    with Database(path) as db:
        db.execute("""
            CREATE TABLE alumnos (id INT, nombre TEXT, nota INT);
            CREATE TABLE materias (nombre TEXT);
            INSERT INTO alumnos VALUES (1, 'Ana', 9), (2, 'Bruno', 6), (3, 'Carla', NULL);
            INSERT INTO materias VALUES ('Algebra');
            UPDATE alumnos SET nota = 10 WHERE id = 1;
            DELETE FROM alumnos WHERE id = 2;
        """)

    with Database(path) as db:
        assert list(db.tables) == ["alumnos", "materias"]
        assert db.query("SELECT * FROM alumnos") == [(1, "Ana", 10), (3, "Carla", None)]
        assert db.query("SELECT * FROM materias") == [("Algebra",)]


def test_el_esquema_se_sigue_respetando_al_reabrir(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE t (n INT, s TEXT)")
    with Database(path) as db:
        assert db.tables["t"].schema() == "CREATE TABLE t (n INT, s TEXT)"
        with pytest.raises(ExecutionError, match="es INT y se le paso TEXT"):
            db.execute("INSERT INTO t VALUES ('x', 'y')")


def test_drop_table_persiste(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE a (n INT); CREATE TABLE b (n INT); DROP TABLE a")
    with Database(path) as db:
        assert list(db.tables) == ["b"]


def test_las_sentencias_anteriores_al_error_quedan_guardadas(path: Path) -> None:
    with Database(path) as db:
        with pytest.raises(ExecutionError):
            db.execute("""
                CREATE TABLE t (n INT);
                INSERT INTO t VALUES (1);
                INSERT INTO t VALUES ('no');
            """)
    with Database(path) as db:
        assert db.query("SELECT * FROM t") == [(1,)]


def test_muchas_filas_y_las_paginas_se_reusan(path: Path) -> None:
    def values(ids: range) -> str:
        return ", ".join(f"({i}, 'alumno numero {i}')" for i in ids)

    with Database(path) as db:
        db.execute("CREATE TABLE t (id INT, nombre TEXT)")
        db.execute(f"INSERT INTO t VALUES {values(range(3000))}")
        assert isinstance(db.storage, DiskStorage)
        pages = db.storage.page_count()
        assert pages > 20

        # borrar la mitad libera paginas y volver a insertarla las reusa,
        # asi que el archivo no crece
        for _ in range(3):
            db.execute("DELETE FROM t WHERE id >= 1500")
            db.execute(f"INSERT INTO t VALUES {values(range(1500, 3000))}")
        assert db.storage.page_count() <= pages + 1

    with Database(path) as db:
        assert db.query("SELECT nombre FROM t WHERE id = 2999") == [("alumno numero 2999",)]
        assert len(db.query("SELECT * FROM t")) == 3000


def test_fila_que_no_entra_en_una_pagina(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE t (s TEXT)")
        with pytest.raises(ExecutionError, match="la fila ocupa"):
            db.execute(f"INSERT INTO t VALUES ('{'x' * 5000}')")
        assert db.query("SELECT * FROM t") == []


def test_archivo_que_no_es_una_base(tmp_path: Path) -> None:
    other = tmp_path / "notas.txt"
    other.write_text("esto no es una base de datos")
    with pytest.raises(ExecutionError, match="no es una base de datos de minidb"):
        Database(other)


def test_usar_la_base_cerrada(path: Path) -> None:
    db = Database(path)
    db.close()
    with pytest.raises(ExecutionError, match="cerrada"):
        db.execute("CREATE TABLE t (n INT)")


def test_consola_con_db_y_script(
    path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "script.sql"
    script.write_text("CREATE TABLE t (n INT); INSERT INTO t VALUES (41 + 1);", encoding="utf-8")
    main(["--db", str(path), str(script)])
    assert "1 fila insertada" in capsys.readouterr().out

    with Database(path) as db:
        assert db.query("SELECT n FROM t") == [(42,)]


def test_sentencia_que_falla_en_el_motor_no_deja_nada(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE t (s TEXT); CREATE INDEX t_s ON t (s)")
        # la primera fila se llega a guardar en el motor y la segunda falla:
        # el rollback tiene que sacar las dos
        with pytest.raises(ExecutionError, match="demasiado largo para el indice t_s"):
            db.execute(f"INSERT INTO t VALUES ('corto'), ('{'x' * 1500}')")
        assert db.query("SELECT * FROM t") == []
        assert db.query("SELECT * FROM t WHERE s = 'corto'") == []
        db.execute("INSERT INTO t VALUES ('otro')")

    with Database(path) as db:
        assert db.query("SELECT * FROM t WHERE s = 'otro'") == [("otro",)]


def test_los_indices_sobreviven_a_cerrar_y_abrir(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE t (n INT, s TEXT); CREATE INDEX t_n ON t (n)")
        values = ", ".join(f"({i}, '{i}')" for i in range(1000))
        db.execute(f"INSERT INTO t VALUES {values}")

    with Database(path) as db:
        assert db.indexes["t_n"].column == "n"
        assert db.query("EXPLAIN SELECT * FROM t WHERE n = 500") == [
            ("SEARCH t USING INDEX t_n (n = 500)",)
        ]
        assert db.query("SELECT s FROM t WHERE n = 500") == [("500",)]


def test_un_journal_incompleto_se_ignora(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE t (n INT); INSERT INTO t VALUES (1)")
    journal = path.with_name(path.name + "-journal")
    journal.write_bytes(b"MDBJRNL1 cortado a la mitad")

    with Database(path) as db:
        assert db.query("SELECT * FROM t") == [(1,)]
    assert not journal.exists()


def test_archivo_del_formato_viejo(path: Path) -> None:
    path.write_bytes(b"MINIDB01" + bytes(8192 - 8))
    with pytest.raises(ExecutionError, match="version vieja"):
        Database(path)
