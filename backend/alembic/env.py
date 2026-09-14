"""Alembic environment — JFW-1 Schema-Linie.

Die DB-URL kommt aus der Umgebung (JFWHISPER_DB_URL), gesetzt von
``init_db()`` bzw. ``backend/schema.py``. Offline-Migrationen sind nicht
unterstuetzt: die Linie laeuft immer gegen die echte SQLite-Datei.
"""

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
file_template = config.get_main_option("file_template")  # noqa: F841 -- Template-Konfiguration

target_metadata = None


def run_migrations_offline() -> None:
    raise NotImplementedError(
        "JFW-1: Offline-Migrationen sind nicht Teil des Laufzeitvertrags."
    )


def run_migrations_online() -> None:
    url = os.environ.get("JFWHISPER_DB_URL")
    if not url:
        raise RuntimeError(
            "JFWHISPER_DB_URL ist nicht gesetzt — init_db()/schema.py muss die URL exportieren."
        )
    connectable = engine_from_config(
        {"sqlalchemy.url": url, "sqlalchemy.poolclass": pool.NullPool},
        prefix="sqlalchemy.",
    )
    with connectable.connect() as connection:
        # JFW-1: Alembic-Batchmodus (Spec) — SQLite braucht Batch, um
        # ALTER-TABLE-Operationen als Table-Rebuild auszufuehren.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
