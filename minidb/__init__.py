from .database import Database, Result
from .errors import DBError, ExecutionError, SQLSyntaxError

__version__ = "0.2.0"

__all__ = ["Database", "Result", "DBError", "ExecutionError", "SQLSyntaxError"]
