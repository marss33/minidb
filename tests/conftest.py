from collections.abc import Iterator
from pathlib import Path

import pytest

from minidb import Database


@pytest.fixture(params=["memoria", "disco"])
def empty(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Database]:
    """Una base vacia. Cada test que la usa corre dos veces: en memoria y en disco."""
    path = tmp_path / "test.db" if request.param == "disco" else None
    with Database(path) as db:
        yield db
