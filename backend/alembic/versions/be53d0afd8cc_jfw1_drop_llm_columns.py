"""JFW-1: LLM-/Refinement-Spalten entfernen (Spec: kein Zielpfad)

Revision ID: be53d0afd8cc
Revises: a765a5987ab2
Create Date: 2026-09-14

Die Initial-Revision ``a765a5987ab2`` wurde in einer fruhen Iteration mit
LLM-/Refinement-Spalten gepusht (auto_refine, llm_model, smart_cleanup,
self_correction, preserve_technical, default_playback_voice_id,
transcript_refined, refinement_flags). Die Spec (JFW-1, Zeile 220) stellt
klar: LLM-/TTS-Felder sind kein Zielpfad -- das Transkript ist Endzustand.

Diese Revision droppt die Spalten idempotent (Batchmodus/SQLite-Rebuild),
damit DBs aus der fruhen Iteration auf das finale Kontrakt konvergieren.
Frische DBs (Initial-Revision ohne LLM-Spalten) sind davon nicht betroffen:
der Drop ist ein No-Op, wenn die Spalten fehlen.
"""

from alembic import op
import sqlalchemy as sa


revision = "be53d0afd8cc"
down_revision = "a765a5987ab2"
branch_labels = None
depends_on = None


# (Tabelle, Spalte) -- exakt die LLM-/Refinement-Spalten der fruhen Iteration.
_LLM_COLUMNS = [
    ("capture_settings", "auto_refine"),
    ("capture_settings", "llm_model"),
    ("capture_settings", "smart_cleanup"),
    ("capture_settings", "self_correction"),
    ("capture_settings", "preserve_technical"),
    ("capture_settings", "default_playback_voice_id"),
    ("captures", "transcript_refined"),
    ("captures", "llm_model"),
    ("captures", "refinement_flags"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    dropped = []
    for table, column in _LLM_COLUMNS:
        if table not in tables:
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if column not in columns:
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column(column)
        dropped.append(f"{table}.{column}")

    # Migrationsbericht (Spec JFW-1, Daten/Migration): Ausschluss von
    # LLM-/Refinement-Inhalten wird ausgewiesen. Die Inhalte sind kein
    # aktives jf-whisper-Produktelement und werden nicht importiert.
    if dropped:
        print(
            "[migration be53d0afd8cc] LLM-/Refinement-Spalten ausgeschlossen "
            "(nicht importiert): " + ", ".join(dropped)
        )


def downgrade() -> None:
    # Kein Downgrade: die Spalten sind aus dem Produkt entfernt (Spec).
    raise NotImplementedError(
        "JFW-1: LLM-/Refinement-Spalten sind kein Zielpfad -- kein Downgrade."
    )
