import pytest

from minidb.errors import SQLSyntaxError
from minidb.tokenizer import TokenType, tokenize


def kinds_and_values(sql: str) -> list[tuple[TokenType, str | int]]:
    return [(t.type, t.value) for t in tokenize(sql)]


def test_select_simple() -> None:
    assert kinds_and_values("SELECT * FROM alumnos;") == [
        (TokenType.KEYWORD, "SELECT"),
        (TokenType.SYMBOL, "*"),
        (TokenType.KEYWORD, "FROM"),
        (TokenType.IDENT, "alumnos"),
        (TokenType.SYMBOL, ";"),
        (TokenType.EOF, ""),
    ]


def test_keywords_y_nombres_no_distinguen_mayusculas() -> None:
    tokens = tokenize("select Nombre FrOm Alumnos")
    assert [t.value for t in tokens[:-1]] == ["SELECT", "nombre", "FROM", "alumnos"]


def test_numeros() -> None:
    assert tokenize("42")[0].value == 42
    assert tokenize("42")[0].type is TokenType.INT


def test_texto_con_comilla_escapada() -> None:
    tok = tokenize("'it''s'")[0]
    assert tok.type is TokenType.STRING
    assert tok.value == "it's"


def test_texto_conserva_mayusculas() -> None:
    assert tokenize("'Hola Mundo'")[0].value == "Hola Mundo"


@pytest.mark.parametrize("symbol", ["<=", ">=", "!=", "<>"])
def test_simbolos_de_dos_caracteres(symbol: str) -> None:
    tokens = tokenize(f"a {symbol} b")
    assert tokens[1].value == symbol


def test_comentarios() -> None:
    tokens = tokenize("SELECT -- esto se ignora\n x FROM t")
    assert [t.value for t in tokens[:-1]] == ["SELECT", "x", "FROM", "t"]


def test_guarda_la_posicion() -> None:
    tokens = tokenize("SELECT  x")
    assert tokens[1].pos == 8


def test_texto_sin_cerrar() -> None:
    with pytest.raises(SQLSyntaxError, match="sin cerrar"):
        tokenize("SELECT 'hola")


def test_caracter_invalido() -> None:
    with pytest.raises(SQLSyntaxError, match="inesperado"):
        tokenize("SELECT # FROM t")
