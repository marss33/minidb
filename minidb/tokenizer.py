"""Convierte el texto SQL en una lista de tokens."""

from dataclasses import dataclass
from enum import Enum, auto

from .errors import SQLSyntaxError


class TokenType(Enum):
    KEYWORD = auto()
    IDENT = auto()
    INT = auto()
    STRING = auto()
    SYMBOL = auto()
    EOF = auto()


KEYWORDS = {
    "SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES", "CREATE", "TABLE", "DROP",
    "DELETE", "UPDATE", "SET", "AND", "OR", "NOT", "IS", "NULL", "ORDER", "BY", "ASC",
    "DESC", "LIMIT", "INT", "TEXT",
}  # fmt: skip

# los de dos caracteres van primero para que "<=" no se lea como "<" y "="
SYMBOLS = ("<=", ">=", "!=", "<>", "=", "<", ">", "(", ")", ",", ";", "*", "+", "-", "/")


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str | int
    pos: int


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(sql)

    while i < n:
        c = sql[i]

        if c.isspace():
            i += 1
            continue

        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            continue

        if c.isdigit():
            start = i
            while i < n and sql[i].isdigit():
                i += 1
            tokens.append(Token(TokenType.INT, int(sql[start:i]), start))
            continue

        if c.isalpha() or c == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i]
            if word.upper() in KEYWORDS:
                tokens.append(Token(TokenType.KEYWORD, word.upper(), start))
            else:
                # los nombres de tablas y columnas no distinguen mayusculas
                tokens.append(Token(TokenType.IDENT, word.lower(), start))
            continue

        if c == "'":
            start = i
            i += 1
            chars: list[str] = []
            while True:
                if i >= n:
                    raise SQLSyntaxError("texto sin cerrar", start)
                if sql[i] == "'":
                    if sql.startswith("''", i):  # '' es una comilla escapada
                        chars.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                chars.append(sql[i])
                i += 1
            tokens.append(Token(TokenType.STRING, "".join(chars), start))
            continue

        for symbol in SYMBOLS:
            if sql.startswith(symbol, i):
                tokens.append(Token(TokenType.SYMBOL, symbol, i))
                i += len(symbol)
                break
        else:
            raise SQLSyntaxError(f"caracter inesperado {c!r}", i)

    tokens.append(Token(TokenType.EOF, "", n))
    return tokens
