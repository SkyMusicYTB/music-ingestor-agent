from sqlalchemy import text


def test_sqlite_uses_rollback_journal_full_sync_and_foreign_keys(engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "delete"
        assert connection.execute(text("PRAGMA synchronous")).scalar_one() == 2
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
