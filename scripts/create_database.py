"""Create only the dedicated CLIP database; never alters the former rag database."""

import os

import psycopg


database = "clip_agent_2026"
admin_dsn = os.environ.get("POSTGRES_ADMIN_DSN")
if not admin_dsn:
    raise SystemExit("Set POSTGRES_ADMIN_DSN to an administrator connection string")
with psycopg.connect(admin_dsn, autocommit=True) as connection:
    exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
    if not exists:
        connection.execute(f'CREATE DATABASE "{database}"')
        print(f"created {database}")
    else:
        print(f"exists {database}")
