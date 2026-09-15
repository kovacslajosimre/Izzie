import sqlite3

import pytest

from app import db


@pytest.fixture
def conn():
    """Migralt, memoriaban elo sqlite kapcsolat - nem erinti a valodi DB-fajlt."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db._migrate(connection)
    yield connection
    connection.close()
