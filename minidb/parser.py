"""Parser de descenso recursivo: tokens -> AST.

Precedencia de menor a mayor:
    OR
    AND
    NOT
    comparaciones (= != < <= > >=, IS NULL)
    + -
    * /
    - unario
    literales, columnas (tabla.columna), funciones (COUNT, SUM...), (parentesis)
"""

from collections.abc import Callable
from typing import TypeVar

from .errors import SQLSyntaxError
from .nodes import (
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
    Expr,
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
from .tokenizer import Token, TokenType, tokenize

T = TypeVar("T")

KW = TokenType.KEYWORD
SYM = TokenType.SYMBOL

COMPARISONS = {"=", "!=", "<>", "<", "<=", ">", ">="}
TYPES = {"INT", "TEXT"}
AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}

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
        if self.keyword("EXPLAIN"):
            statement = self.parse_statement()
            if not isinstance(statement, Select | Update | Delete):
                raise SQLSyntaxError("EXPLAIN solo funciona con SELECT, UPDATE y DELETE")
            return Explain(statement)
        if self.keyword("BEGIN"):
            self.keyword("TRANSACTION")
            return Begin()
        if self.keyword("COMMIT"):
            self.keyword("TRANSACTION")
            return Commit()
        if self.keyword("ROLLBACK"):
            self.keyword("TRANSACTION")
            return Rollback()
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
            if self.keyword("INDEX"):
                return DropIndex(self.ident())
            self.expect(KW, "TABLE")
            return DropTable(self.ident())
        raise self.error(
            "se esperaba SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, EXPLAIN o BEGIN"
        )

    def parse_create(self) -> CreateTable | CreateIndex:
        unique = self.keyword("UNIQUE")
        if unique or self.keyword("INDEX"):
            if unique:
                self.expect(KW, "INDEX")
            name = self.ident()
            self.expect(KW, "ON")
            table = self.ident()
            self.expect(SYM, "(")
            column = self.ident()
            self.expect(SYM, ")")
            return CreateIndex(name, table, column, unique)
        self.expect(KW, "TABLE")
        table = self.ident()
        self.expect(SYM, "(")
        columns = self.comma_list(self.parse_column_def)
        self.expect(SYM, ")")
        return CreateTable(table, tuple(columns))

    def parse_column_def(self) -> ColumnDef:
        name = self.ident()
        tok = self.peek()
        if tok.type is not KW or tok.value not in TYPES:
            raise self.error("se esperaba un tipo (INT o TEXT)")
        self.advance()
        primary_key = unique = not_null = False
        while True:
            if self.keyword("PRIMARY"):
                self.expect(KW, "KEY")
                primary_key = True
            elif self.keyword("UNIQUE"):
                unique = True
            elif self.keyword("NOT"):
                self.expect(KW, "NULL")
                not_null = True
            else:
                return ColumnDef(name, str(tok.value), primary_key, unique, not_null)

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
        columns = None if self.symbol("*") else tuple(self.comma_list(self.parse_select_item))
        self.expect(KW, "FROM")
        table = self.parse_table_ref()
        joins = []
        while join := self.parse_join():
            joins.append(join)
        where = self.parse_where()

        group_by: tuple[Column, ...] = ()
        if self.keyword("GROUP"):
            self.expect(KW, "BY")
            group_by = tuple(self.comma_list(self.parse_column))
        having = self.parse_expr() if self.keyword("HAVING") else None

        order_by: tuple[OrderBy, ...] = ()
        if self.keyword("ORDER"):
            self.expect(KW, "BY")
            order_by = tuple(self.comma_list(self.parse_order_item))

        limit = None
        if self.keyword("LIMIT"):
            limit = int(self.expect(TokenType.INT).value)

        return Select(table, columns, where, order_by, limit, tuple(joins), group_by, having)

    def parse_select_item(self) -> SelectItem:
        expr = self.parse_expr()
        alias = self.ident() if self.keyword("AS") else None
        return SelectItem(expr, alias)

    def parse_table_ref(self) -> TableRef:
        name = self.ident()
        if self.keyword("AS"):
            return TableRef(name, self.ident())
        alias = self.match(TokenType.IDENT)
        return TableRef(name, None if alias is None else str(alias.value))

    def parse_join(self) -> Join | None:
        left = False
        if self.keyword("LEFT"):
            self.keyword("OUTER")
            left = True
        elif not self.keyword("INNER") and not self.check(KW, "JOIN"):
            return None
        self.expect(KW, "JOIN")
        table = self.parse_table_ref()
        self.expect(KW, "ON")
        return Join(table, self.parse_expr(), left)

    def parse_column(self) -> Column:
        name = self.ident()
        if self.symbol("."):
            return Column(self.ident(), table=name)
        return Column(name)

    def parse_order_item(self) -> OrderBy:
        expr = self.parse_expr()
        if self.keyword("DESC"):
            return OrderBy(expr, descending=True)
        self.keyword("ASC")
        return OrderBy(expr)

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
            following = self.tokens[self.pos + 1]
            if following.type is SYM and following.value == "(":
                return self.parse_function()
            return self.parse_column()
        if self.keyword("NULL"):
            return Literal(None)
        if self.symbol("("):
            expr = self.parse_expr()
            self.expect(SYM, ")")
            return expr
        raise self.error("se esperaba un valor o una columna")

    def parse_function(self) -> Aggregate:
        tok = self.advance()
        name = str(tok.value).upper()
        if name not in AGGREGATES:
            raise SQLSyntaxError(f"no existe la funcion {tok.value}", tok.pos)
        self.expect(SYM, "(")
        if name == "COUNT" and self.symbol("*"):
            arg = None
        else:
            arg = self.parse_expr()
        self.expect(SYM, ")")
        return Aggregate(name, arg)
