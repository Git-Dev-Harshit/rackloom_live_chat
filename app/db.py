"""Connection pool and the SQL migration runner.

Migrations are the plain .sql files in ../migrations, applied in filename order and recorded in
support_schema_migrations, the same scheme as the auth service (auth/internal/migrate). No down
migrations, nothing ever dropped.
"""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("support.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
APP_ROLE = "support_app"


def open_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    """Each `with pool.connection()` block is one transaction: committed at the end, rolled back on error."""
    pool = ConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "options": "-c TimeZone=UTC"},
        timeout=10,
        open=False,
        name="support",
    )
    pool.open(wait=True, timeout=30)
    return pool


def migrate(owner_url: str, app_password: str = "") -> list[str]:
    """Apply pending migrations as the owner; set the app role's password when one is given."""
    applied: list[str] = []
    with psycopg.connect(owner_url, autocommit=True) as c:
        # One migrator at a time, even if two deploys race.
        c.execute("select pg_advisory_lock(hashtext('support_schema_migrations'))")
        try:
            c.execute("create table if not exists support_schema_migrations (version text primary key, applied_at timestamptz not null default now())")
            done = {r[0] for r in c.execute("select version from support_schema_migrations").fetchall()}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = path.stem
                if version in done:
                    continue
                with c.transaction():
                    c.execute(path.read_text(encoding="utf-8"))
                    c.execute("insert into support_schema_migrations (version) values (%s)", (version,))
                applied.append(version)
                log.info("migration applied: %s", version)
            if app_password:
                c.execute(sql.SQL("alter role {} with login password {}").format(sql.Identifier(APP_ROLE), sql.Literal(app_password)))
                log.info("password set for role %s", APP_ROLE)
        finally:
            c.execute("select pg_advisory_unlock(hashtext('support_schema_migrations'))")
    return applied
