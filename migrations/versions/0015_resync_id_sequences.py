"""Подтянуть счётчики id (serial-последовательности) до max(id) таблиц

2026-11-02 после перезапуска базы вставка в intake_requests упала с
IntegrityError, хотя строки для поста не было: похоже, последовательность
id отстала от данных. Без доступа к базе чиним миграцией.

Для каждой последовательности, принадлежащей колонке таблицы, ставим
``max(колонка)``, только когда счётчик не впереди данных: если он в порядке,
миграция отрабатывает вхолостую. На SQLite последовательностей
нет, там ничего не делаем.

Revision ID: 0015
Revises: 0014

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RESYNC = """
DO $$
DECLARE
    r record;
    max_id bigint;
    last_id bigint;
BEGIN
    FOR r IN
        SELECT s.oid::regclass::text AS seq,
               t.oid::regclass::text AS tbl,
               quote_ident(a.attname) AS col
        FROM pg_class s
        JOIN pg_depend d ON d.objid = s.oid
                        AND d.classid = 'pg_class'::regclass
                        AND d.deptype IN ('a', 'i')
        JOIN pg_class t ON t.oid = d.refobjid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE s.relkind = 'S' AND n.nspname = current_schema()
    LOOP
        EXECUTE format('SELECT max(%s) FROM %s', r.col, r.tbl) INTO max_id;
        EXECUTE format('SELECT last_value FROM %s', r.seq) INTO last_id;
        IF max_id IS NOT NULL AND max_id >= last_id THEN
            RAISE NOTICE 'resync % : % -> %', r.seq, last_id, max_id;
            PERFORM setval(r.seq, max_id);
        END IF;
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_RESYNC)


def downgrade() -> None:
    # Откатывать счётчик назад нельзя — это и было поломкой.
    pass
