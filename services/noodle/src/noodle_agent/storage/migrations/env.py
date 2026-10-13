from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config


def migrate(connection):
    context.configure(connection=connection, transactional_ddl=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("SQLite migrations require an online connection for legacy-schema checks")
elif (connection := config.attributes.get("connection")) is not None:
    migrate(connection)
else:
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        migrate(connection)
        connection.commit()
