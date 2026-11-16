class DBError(Exception):
    """Error base de minidb."""


class SQLSyntaxError(DBError):
    def __init__(self, message: str, pos: int | None = None) -> None:
        self.pos = pos
        if pos is not None:
            message = f"{message} (posicion {pos})"
        super().__init__(message)


class ExecutionError(DBError):
    pass
