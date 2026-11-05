"""SQLite databases shared by the api, orchestrator and supervisor processes.

There are two files, on purpose:

- ``foreman.db`` holds projects, tasks, runs and events. Everyone writes it.
- ``control.db`` holds stop requests and the E-stop flag. Only the API and the
  supervisor write it; the orchestrator only reads it.

The split keeps the stop path independent of the orchestrator: if the
orchestrator hangs (or is frozen) in the middle of a write transaction it holds
foreman.db's write lock, but a Stop can still be recorded in control.db and
acted on by the supervisor. Both run in WAL mode, so readers never wait for a
writer.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from pydantic_core import PydanticUndefined
from sqlalchemy import Table, event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from foreman.config import get_settings
from foreman.core import models

CONTROL_TABLES: list[Table] = [
    models.Control.__table__,  # type: ignore[attr-defined]
    models.SystemFlag.__table__,  # type: ignore[attr-defined]
]


def _main_tables() -> list[Table]:
    return [t for t in SQLModel.metadata.sorted_tables if t not in CONTROL_TABLES]


@cache
def _engine_for(path: str, control: bool) -> Engine:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    tables = CONTROL_TABLES if control else _main_tables()
    SQLModel.metadata.create_all(engine, tables=tables)
    _add_missing_columns(engine, tables)
    if not control:
        with engine.begin() as conn:  # "gateway" was the earlier name for "isolated"
            conn.exec_driver_sql(
                "UPDATE project SET network = 'isolated' WHERE network = 'gateway'"
            )
    return engine


def _table_models() -> dict[str, type[SQLModel]]:
    out: dict[str, type[SQLModel]] = {}
    stack = list(SQLModel.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        table = getattr(cls, "__table__", None)
        if table is not None:
            out[table.name] = cls
    return out


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list | dict):
        value = json.dumps(value)
    return "'" + str(value).replace("'", "''") + "'"


def _add_missing_columns(engine: Engine, tables: list[Table]) -> None:
    """Minimal forward migration: add columns introduced by newer versions.

    create_all() creates missing tables but never alters existing ones, so a database
    from an older foreman would lack new columns. New columns are only ever added
    with defaults, which this handles; anything more needs a real migration.
    """
    models = _table_models()
    with engine.begin() as conn:
        for table in tables:
            existing = {
                row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')
            }
            model = models.get(table.name)
            for column in table.columns:
                if column.name in existing:
                    continue
                default: object = None
                field = model.model_fields.get(column.name) if model else None
                if field is not None:
                    if field.default_factory is not None:
                        default = field.default_factory()  # type: ignore[call-arg]
                    elif field.default is not PydanticUndefined:
                        default = field.default
                if isinstance(default, enum.Enum):  # SQLAlchemy stores enum *names*
                    default = default.name
                col_type = column.type.compile(dialect=engine.dialect)
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type} '
                    f"DEFAULT {_sql_literal(default)}"
                )


def get_engine() -> Engine:
    return _engine_for(str(get_settings().db_path), False)


def get_control_engine() -> Engine:
    return _engine_for(str(get_settings().control_db_path), True)


@contextmanager
def session() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as s:
        yield s


@contextmanager
def control_session() -> Iterator[Session]:
    with Session(get_control_engine(), expire_on_commit=False) as s:
        yield s


def reset_engine_cache() -> None:
    """For tests that switch FOREMAN_DATA_DIR."""
    _engine_for.cache_clear()
