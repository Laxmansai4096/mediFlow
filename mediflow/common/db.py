"""Database helper. Each microservice owns its own database.

Local: one SQLite file per service.  Azure: set <SERVICE>_DATABASE_URL to an
Azure SQL connection string, e.g.
  mssql+pyodbc://@server.database.windows.net/mediflow?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi
(passwordless, via the Container App's managed identity).
"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


def database_url(service: str) -> str:
    return config.env(f"{service.upper()}_DATABASE_URL") or (
        f"sqlite:///{(config.data_dir() / f'{service}.db').as_posix()}"
    )


class Database:
    def __init__(self, service: str, base: type[DeclarativeBase]):
        url = database_url(service)
        is_sqlite = url.startswith("sqlite")
        self.engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30} if is_sqlite else {},
            pool_pre_ping=True,
        )
        if is_sqlite:
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.db.Session() as session:
        yield session
