from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event

import manga_localizer.database as database_module
from manga_localizer.database import (
    ImageAsset,
    PageGeneration,
    PageTranslationReview,
    Project,
    RegionTranslationCandidate,
    Revision,
    SessionFactory,
    TextRegion,
    create_project_engine,
)


def _schema(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return list(
        connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        )
    )


def _row_digest(connection: sqlite3.Connection, table: str) -> str:
    rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def _replace_constraint(create_sql: str, old_columns: tuple[str, ...], replacement: str) -> str:
    column_pattern = r"\s*,\s*".join(rf'["`\[]?{column}["`\]]?' for column in old_columns)
    pattern = (
        r"CONSTRAINT\s+(?:\"[^\"]+\"|`[^`]+`|\[[^]]+\]|\S+)\s+"
        rf"UNIQUE\s*\(\s*{column_pattern}\s*\)"
    )
    result, count = re.subn(pattern, replacement, create_sql, flags=re.IGNORECASE)
    assert count == 1, create_sql
    return result


def _make_legacy_database(path: Path) -> dict[str, object]:
    engine = create_project_engine(path)
    with engine.begin() as connection:
        for trigger in (
            "region_translation_candidates_validate_insert",
            "page_translation_reviews_validate_insert",
        ):
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{trigger}"')
    session_factory = SessionFactory(bind=engine)
    with session_factory() as session:
        project = Project(id="project", name="fixture", root_path="/fixture")
        image = ImageAsset(
            id="image",
            project_id="project",
            name="page.png",
            relative_path="page.png",
            source_path="page.png",
            width=100,
            height=200,
            checksum="1" * 64,
        )
        generation = PageGeneration(
            id="generation",
            run_id="run",
            project_id="project",
            image_id="image",
            restart_from_source=False,
            parameter_set_id="parameters",
            parameter_set_hash="2" * 64,
            source_project_id="project",
            source_image_id="image",
            source_checksum="1" * 64,
            source_relative_path="page.png",
            actor_kind="agent",
            operation_source="test",
        )
        region = TextRegion(
            id="region",
            image_id="image",
            x=1,
            y=2,
            width=3,
            height=4,
            source_text="原文",
        )
        revision = Revision(
            id="revision",
            project_id="project",
            entity_type="region",
            entity_id="region",
            operation="create",
            project_revision=1,
        )
        candidate = RegionTranslationCandidate(
            id="candidate",
            generation_id="generation",
            image_id="image",
            region_id="region",
            sequence=1,
            revision_number=1,
            origin_kind="manual",
            g8_checksum="8" * 64,
            clean_plate_checksum="8" * 64,
            source_text_checksum="3" * 64,
            source_region_revision=1,
            context_checksum="4" * 64,
            context_policy={"scope": "page"},
            provider="human",
            model_version="manual",
            parameter_hash="5" * 64,
            target_language="zh-CN",
            translation_text="译文",
            candidate_checksum="6" * 64,
            computed_qc_flags=[],
            revision_id="revision",
        )
        terminal = PageTranslationReview(
            id="terminal",
            generation_id="generation",
            image_id="image",
            sequence=2,
            state="accepted",
            reason="accepted",
            g8_checksum="8" * 64,
            translation_state_checksum="7" * 64,
            terminal_checksum="9" * 64,
            accepted_candidate_ids=["candidate"],
            reviewer={"kind": "agent"},
            revision_id="revision",
        )
        session.add(project)
        session.flush()
        session.add(image)
        session.flush()
        session.add_all([generation, region, revision])
        session.flush()
        session.add(candidate)
        session.flush()
        session.add(terminal)
        session.commit()
    engine.dispose()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE INDEX preserved_candidate_source "
            "ON region_translation_candidates(source_text_checksum)"
        )
        connection.execute(
            """
            CREATE TRIGGER preserved_translation_guard
            BEFORE UPDATE ON page_translation_reviews
            BEGIN
                SELECT RAISE(ABORT, 'preserved review guard');
            END
            """
        )
        definitions = dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN "
                "('region_translation_candidates', 'page_translation_reviews')"
            )
        )
        candidate_sql = _replace_constraint(
            definitions["region_translation_candidates"],
            ("generation_id", "region_id", "g8_checksum", "revision_number"),
            'CONSTRAINT "renamed candidate epoch" '
            'UNIQUE ("generation_id", "region_id", "revision_number")',
        )
        terminal_sql = _replace_constraint(
            definitions["page_translation_reviews"],
            ("generation_id", "g8_checksum"),
            "CONSTRAINT [renamed terminal epoch] UNIQUE ([generation_id])",
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for table, replacement_sql in (
            ("region_translation_candidates", candidate_sql),
            ("page_translation_reviews", terminal_sql),
        ):
            temporary = f"{table}__legacy_fixture"
            schema_objects = connection.execute(
                """
                SELECT type, name, sql FROM sqlite_master
                WHERE sql IS NOT NULL AND (
                    (tbl_name = ? AND type IN ('index', 'trigger'))
                    OR (type = 'trigger' AND sql LIKE ?)
                )
                ORDER BY type, name
                """,
                (table, f"%{table}%"),
            ).fetchall()
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            connection.execute(
                f'CREATE TABLE "{temporary}" ' + replacement_sql[replacement_sql.find("(") :]
            )
            connection.execute(
                f'INSERT INTO "{temporary}" ({quoted_columns}) '
                f'SELECT {quoted_columns} FROM "{table}"'
            )
            for object_type, object_name, _ in schema_objects:
                if object_type == "trigger":
                    connection.execute(f'DROP TRIGGER "{object_name}"')
            connection.execute(f'DROP TABLE "{table}"')
            connection.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
            for _, _, object_sql in schema_objects:
                connection.execute(object_sql)
        connection.execute("DROP INDEX uq_typeset_terminal_g9")
        connection.execute(
            'CREATE UNIQUE INDEX "renamed old g10 epoch" '
            'ON page_typeset_reviews("generation_id") WHERE ("state" = \'accepted\')'
        )
        connection.execute(
            'CREATE UNIQUE INDEX "renamed old g8 epoch" '
            'ON page_cloud_full_page_reviews("generation_id") WHERE ("state" = \'accepted\')'
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        return {
            "candidate": _row_digest(connection, "region_translation_candidates"),
            "terminal": _row_digest(connection, "page_translation_reviews"),
            "foreignKeys": {
                table: list(connection.execute(f'PRAGMA foreign_key_list("{table}")'))
                for table in ("region_translation_candidates", "page_translation_reviews")
            },
            "preservedObjects": {
                name: sql
                for name, sql in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name IN "
                    "('preserved_candidate_source', 'preserved_translation_guard')"
                )
            },
        }


def _unique_shapes(connection: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    shapes: set[tuple[str, ...]] = set()
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        if row[2]:
            shapes.add(
                tuple(
                    info[2]
                    for info in connection.execute(f'PRAGMA index_info("{row[1]}")')
                    if info[2] is not None
                )
            )
    return shapes


def test_legacy_epoch_migration_preserves_rows_relations_and_schema_objects(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    before = _make_legacy_database(path)

    engine = create_project_engine(path)
    engine.dispose()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _row_digest(connection, "region_translation_candidates") == before["candidate"]
        assert _row_digest(connection, "page_translation_reviews") == before["terminal"]
        assert {
            table: list(connection.execute(f'PRAGMA foreign_key_list("{table}")'))
            for table in ("region_translation_candidates", "page_translation_reviews")
        } == before["foreignKeys"]
        assert {
            name: sql
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name IN "
                "('preserved_candidate_source', 'preserved_translation_guard')"
            )
        } == before["preservedObjects"]
        assert ("generation_id", "region_id", "revision_number") not in _unique_shapes(
            connection, "region_translation_candidates"
        )
        assert (
            "generation_id",
            "region_id",
            "g8_checksum",
            "revision_number",
        ) in _unique_shapes(connection, "region_translation_candidates")
        assert ("generation_id",) not in _unique_shapes(connection, "page_translation_reviews")
        assert ("generation_id", "g8_checksum") in _unique_shapes(
            connection, "page_translation_reviews"
        )
        assert ("generation_id",) not in _unique_shapes(connection, "page_cloud_full_page_reviews")
        assert ("generation_id", "g9_terminal_checksum") in _unique_shapes(
            connection, "page_typeset_reviews"
        )

        connection.execute("DROP TRIGGER region_translation_candidates_validate_insert")
        connection.execute("DROP TRIGGER page_translation_reviews_validate_insert")
        connection.execute(
            """
            INSERT INTO region_translation_candidates
            SELECT 'candidate-new-g8', generation_id, image_id, region_id, sequence,
                   revision_number, supersedes_candidate_id, origin_kind, ?,
                   clean_plate_checksum, source_text_checksum, source_region_revision,
                   context_checksum, context_policy, provider, model_version, parameter_hash,
                   target_language, translation_text, ?, computed_qc_flags, job_id, job_item_id,
                   revision_id, created_at
            FROM region_translation_candidates WHERE id = 'candidate'
            """,
            ("a" * 64, "b" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO region_translation_candidates
                SELECT 'candidate-duplicate', generation_id, image_id, region_id, sequence,
                       revision_number, supersedes_candidate_id, origin_kind, g8_checksum,
                       clean_plate_checksum, source_text_checksum, source_region_revision,
                       context_checksum, context_policy, provider, model_version, parameter_hash,
                       target_language, translation_text, ?, computed_qc_flags, job_id,
                       job_item_id, revision_id, created_at
                FROM region_translation_candidates WHERE id = 'candidate'
                """,
                ("c" * 64,),
            )
        connection.execute(
            """
            INSERT INTO page_translation_reviews
            SELECT 'terminal-new-g8', generation_id, image_id, sequence, state, reason, ?,
                   translation_state_checksum, ?, accepted_candidate_ids, reviewer, revision_id,
                   created_at
            FROM page_translation_reviews WHERE id = 'terminal'
            """,
            ("a" * 64, "b" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO page_translation_reviews
                SELECT 'terminal-duplicate', generation_id, image_id, sequence, state, reason,
                       g8_checksum, translation_state_checksum, ?, accepted_candidate_ids,
                       reviewer, revision_id, created_at
                FROM page_translation_reviews WHERE id = 'terminal'
                """,
                ("c" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE region_translation_candidates SET provider = provider "
                "WHERE id = 'candidate'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="preserved review guard"):
            connection.execute(
                "UPDATE page_translation_reviews SET reason = reason WHERE id = 'terminal'"
            )


def test_epoch_migration_preserves_unrelated_predicates_and_requires_full_g9_uniqueness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "partial-indexes.sqlite3"
    _make_legacy_database(path)
    statements = (
        "CREATE UNIQUE INDEX custom_rejected_generation ON page_cloud_full_page_reviews"
        "(generation_id) WHERE state = 'rejected'",
        "CREATE UNIQUE INDEX custom_manual_revision ON region_translation_candidates"
        "(generation_id, region_id, revision_number) WHERE origin_kind = 'manual'",
        "CREATE UNIQUE INDEX custom_manual_epoch ON region_translation_candidates"
        "(generation_id, region_id, g8_checksum, revision_number) WHERE origin_kind = 'manual'",
        "CREATE UNIQUE INDEX custom_accepted_terminal ON page_translation_reviews"
        "(generation_id) WHERE state = 'accepted'",
        "CREATE UNIQUE INDEX custom_accepted_epoch ON page_translation_reviews"
        "(generation_id, g8_checksum) WHERE state = 'accepted'",
    )
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
        before = dict(
            connection.execute("SELECT name, sql FROM sqlite_master WHERE name LIKE 'custom_%'")
        )

    engine = create_project_engine(path)
    engine.dispose()

    with sqlite3.connect(path) as connection:
        assert (
            dict(
                connection.execute("SELECT name, sql FROM sqlite_master WHERE name LIKE 'custom_%'")
            )
            == before
        )
        for table, expected in (
            (
                "region_translation_candidates",
                ("generation_id", "region_id", "g8_checksum", "revision_number"),
            ),
            ("page_translation_reviews", ("generation_id", "g8_checksum")),
        ):
            full_unique_shapes = {
                tuple(column[2] for column in connection.execute(f'PRAGMA index_info("{row[1]}")'))
                for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall()
                if row[2] and not row[4]
            }
            assert expected in full_unique_shapes
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_epoch_migration_rolls_back_and_retries_after_trigger_restore_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.sqlite3"
    _make_legacy_database(path)
    with sqlite3.connect(path) as connection:
        before_schema = _schema(connection)
        before_rows = (
            _row_digest(connection, "region_translation_candidates"),
            _row_digest(connection, "page_translation_reviews"),
        )

    original_create_engine = database_module.create_engine
    deny_restore = True

    def instrumented_create_engine(*args, **kwargs):
        engine = original_create_engine(*args, **kwargs)

        @event.listens_for(engine, "connect")
        def install_authorizer(dbapi_connection, _record) -> None:
            def authorize(action, argument, _arg2, _database, _source):
                if (
                    deny_restore
                    and action == sqlite3.SQLITE_CREATE_TRIGGER
                    and argument == "preserved_translation_guard"
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            dbapi_connection.set_authorizer(authorize)

        return engine

    monkeypatch.setattr(database_module, "create_engine", instrumented_create_engine)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        create_project_engine(path)

    with sqlite3.connect(path) as connection:
        assert _schema(connection) == before_schema
        assert (
            _row_digest(connection, "region_translation_candidates"),
            _row_digest(connection, "page_translation_reviews"),
        ) == before_rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    deny_restore = False
    engine = create_project_engine(path)
    engine.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_epoch_migration_refuses_unknown_residual_table_without_dropping_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "residual.sqlite3"
    _make_legacy_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE region_translation_candidates__epoch_migration (sentinel TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO region_translation_candidates__epoch_migration VALUES ('keep-me')"
        )
        connection.commit()
        before_schema = _schema(connection)

    with pytest.raises(RuntimeError, match="refusing to overwrite residual table"):
        create_project_engine(path)

    with sqlite3.connect(path) as connection:
        assert _schema(connection) == before_schema
        assert connection.execute(
            "SELECT sentinel FROM region_translation_candidates__epoch_migration"
        ).fetchall() == [("keep-me",)]


def test_current_epoch_schema_reopen_emits_no_migration_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "current.sqlite3"
    engine = create_project_engine(path)
    engine.dispose()
    statements: list[str] = []
    original_create_engine = database_module.create_engine

    def traced_create_engine(*args, **kwargs):
        traced_engine = original_create_engine(*args, **kwargs)

        @event.listens_for(traced_engine, "connect")
        def install_trace(dbapi_connection, _record) -> None:
            dbapi_connection.set_trace_callback(statements.append)

        return traced_engine

    monkeypatch.setattr(database_module, "create_engine", traced_create_engine)
    engine = create_project_engine(path)
    engine.dispose()

    epoch_ddl = [
        statement
        for statement in statements
        if re.search(
            r"(?i)^(?:CREATE|DROP|ALTER).*"
            r"(?:__epoch_migration|uq_translation_candidate_revision_g8|"
            r"uq_translation_terminal_generation_g8|uq_typeset_terminal_g9|"
            r"renamed old g8 epoch)",
            statement.strip(),
        )
    ]
    assert epoch_ddl == []
