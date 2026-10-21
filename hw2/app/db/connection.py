import os
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL


@lru_cache
def get_engine() -> Engine:
    """Create the shared pool on first DB use, not while importing routes."""
    database_url = URL.create(
        drivername="postgresql+psycopg",
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ["DB_NAME"],
    )
    return create_engine(database_url, pool_pre_ping=True)
