"""Parser de descenso recursivo: tokens -> AST.

Precedencia de menor a mayor:
    OR
    AND
    NOT
    comparaciones (= != < <= > >=, IS NULL)
    + -
    * /
    - unario
    literales, columnas, (parentesis)
"""

from collections.abc import Callable
from typing import TypeVar

from .errors import SQLSyntaxError
from .nodes import (
    BinaryOp,
    Column,
    ColumnDef,
    CreateTable,
    Delete,
    DropTable,
    Expr,
    Insert,
    IsNull,
    Literal,
    OrderBy,
    Select,
    Statement,
    UnaryOp,
    Update,
)
from .tokenizer import Token, TokenType, tokenize

T = TypeVar("T")

KW = TokenType.KEYWORD
SYM = TokenType.SYMBOL

COMPARISONS = {"=", "!=", "<>", "<", "<=", ">", ">="}
TYPES = {"INT", "TEXT"}

DESCRIPTIONS = {
    TokenType.IDENT: "un nombre",
    TokenType.INT: "un numero",
    TokenType.STRING: "un texto",
}


def parse(sql: str) -> list[Statement]:
    return Parser(tokenize(sql)).parse_script()


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # ---- helpers ----

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type is not TokenType.EOF:
            self.pos += 1
        return tok

    def check(self, type_: TokenType, value: str | None = None) -> bool:
        tok = self.peek()
        return tok.type is type_ and (value is None or tok.value == value)

    def match(self, type_: TokenType, value: str | None = None) -> Token | None:
        if self.check(type_, value):
            return self.advance()
        return None

    def expect(self, type_: TokenType, value: str | None = None) -> Token:
        tok = self.match(type_, value)
        if tok is None:
            wanted = value if value is not None else DESCRIPTIONS.get(type_, type_.name)
            raise self.error(f"se esperaba {wanted}")
        return tok

    def error(self, message: str) -> SQLSyntaxError:
        tok = self.peek()
        got = "el final" if tok.type is TokenType.EOF else repr(tok.value)
        return SQLSyntaxError(f"{message}, pero vino {got}", tok.pos)

    def keyword(self, kw: str) -> bool:
        return self.match(KW, kw) is not None

    def symbol(self, s: str) -> bool:
        return self.match(SYM, s) is not None

    def ident(self) -> str:
        return str(self.expect(TokenType.IDENT).value)

    def comma_list(self, item: Callable[[], T]) -> list[T]:
        items = [item()]
        while self.symbol(","):
            items.append(item())
        return items

    # ---- sentencias ----

    def parse_script(self) -> list[Statement]:
        statements: list[Statement] = []
        while not self.check(TokenType.EOF):
            if self.symbol(";"):
                continue
            statements.append(self.parse_statement())
            if not self.check(TokenType.EOF):
                self.expect(SYM, ";")
        return statements

    def parse_statement(self) -> Statement:
        if self.keyword("SELECT"):
            return self.parse_select()
        if self.keyword("INSERT"):
            return self.parse_insert()
        if self.keyword("UPDATE"):
            return self.parse_update()
        if self.keyword("DELETE"):
            return self.parse_delete()
        if self.keyword("CREATE"):
            return self.parse_create()
        if self.keyword("DROP"):
            self.expect(KW, "TABLE")
            return DropTable(self.ident())
        raise self.error("se esperaba SELECT, INSERT, UPDATE, DELETE, CREATE o DROP")

    def parse_create(self) -> CreateTable:
        self.expect(KW, "TABLE")
        table = self.ident()
        self.expect(SYM, "(")
        columns = self.comma_list(self.parse_column_def)
        self.expect(SYM, ")")
        return CreateTable(table, tuple(columns))

    def parse_column_def(self) -> ColumnDef:
        name = self.ident()
        tok = self.peek()
        if tok.type is KW and tok.value in TYPES:
            self.advance()
            return ColumnDef(name, str(tok.value))
        raise self.error("se esperaba un tipo (INT o TEXT)")

    def parse_insert(self) -> Insert:
        self.expect(KW, "INTO")
        table = self.ident()
        columns = None
        if self.symbol("("):
            columns = tuple(self.comma_list(self.ident))
            self.expect(SYM, ")")
        self.expect(KW, "VALUES")
        rows = self.comma_list(self.parse_values_row)
        return Insert(table, columns, tuple(rows))

    def parse_values_row(self) -> tuple[Expr, ...]:
        self.expect(SYM, "(")
        values = self.comma_list(self.parse_expr)
        self.expect(SYM, ")")
        return tuple(values)

    def parse_select(self) -> Select:
        columns = None if self.symbol("*") else tuple(self.comma_list(self.ident))
        self.expect(KW, "FROM")
        table = self.ident()
        where = self.parse_where()

        order_by: tuple[OrderBy, ...] = ()
        if self.keyword("ORDER"):
            self.expect(KW, "BY")
            order_by = tuple(self.comma_list(self.parse_order_item))

        limit = None
        if self.keyword("LIMIT"):
            limit = int(self.expect(TokenType.INT).value)

        return Select(table, columns, where, order_by, limit)

    def parse_order_item(self) -> OrderBy:
        column = self.ident()
        if self.keyword("DESC"):
            return OrderBy(column, descending=True)
        self.keyword("ASC")
        return OrderBy(column)

    def parse_update(self) -> Update:
        table = self.ident()
        self.expect(KW, "SET")
        assignments = self.comma_list(self.parse_assignment)
        return Update(table, tuple(assignments), self.parse_where())

    def parse_assignment(self) -> tuple[str, Expr]:
        column = self.ident()
        self.expect(SYM, "=")
        return column, self.parse_expr()

    def parse_delete(self) -> Delete:
        self.expect(KW, "FROM")
        table = self.ident()
        return Delete(table, self.parse_where())

    def parse_where(self) -> Expr | None:
        return self.parse_expr() if self.keyword("WHERE") else None

    # ---- expresiones ----

    def parse_expr(self) -> Expr:
        return self.parse_or()

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.keyword("OR"):
            left = BinaryOp("OR", left, self.parse_and())
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.keyword("AND"):
            left = BinaryOp("AND", left, self.parse_not())
        return left

    def parse_not(self) -> Expr:
        if self.keyword("NOT"):
            return UnaryOp("NOT", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        left = self.parse_additive()

        if self.keyword("IS"):
            negated = self.keyword("NOT")
            self.expect(KW, "NULL")
            return IsNull(left, negated)

        tok = self.peek()
        if tok.type is SYM and tok.value in COMPARISONS:
            self.advance()
            op = "!=" if tok.value == "<>" else str(tok.value)
            return BinaryOp(op, left, self.parse_additive())
        return left

    def parse_additive(self) -> Expr:
        left = self.parse_multiplicative()
        while (tok := self.peek()).type is SYM and tok.value in ("+", "-"):
            self.advance()
            left = BinaryOp(str(tok.value), left, self.parse_multiplicative())
        return left

    def parse_multiplicative(self) -> Expr:
        left = self.parse_unary()
        while (tok := self.peek()).type is SYM and tok.value in ("*", "/"):
            self.advance()
            left = BinaryOp(str(tok.value), left, self.parse_unary())
        return left

    def parse_unary(self) -> Expr:
        if self.symbol("-"):
            operand = self.parse_unary()
            # -5 queda como literal directamente
            if isinstance(operand, Literal) and type(operand.value) is int:
                return Literal(-operand.value)
            return UnaryOp("-", operand)
        return self.parse_primary()

    def parse_primary(self) -> Expr:
        tok = self.peek()
        if tok.type in (TokenType.INT, TokenType.STRING):
            self.advance()
            return Literal(tok.value)
        if tok.type is TokenType.IDENT:
            self.advance()
            return Column(str(tok.value))
        if self.keyword("NULL"):
            return Literal(None)
        if self.symbol("("):
            expr = self.parse_expr()
            self.expect(SYM, ")")
            return expr
        raise self.error("se esperaba un valor o una columna")
