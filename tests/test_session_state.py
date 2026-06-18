from __future__ import annotations

from pathlib import Path

from bub.builtin.session_state import SessionStateStore, sanitise_session_id


def test_load_returns_empty_for_missing_session(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)

    assert store.load("never-seen") == {}


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)

    store.save("slack:C123", {"model": "openai:gpt-4o"})

    assert store.load("slack:C123") == {"model": "openai:gpt-4o"}


def test_save_overwrites_existing_value(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store.save("session", {"model": "openai:gpt-4o"})

    store.save("session", {"model": "anthropic:claude-3"})

    assert store.load("session") == {"model": "anthropic:claude-3"}


def test_delete_removes_persisted_data(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store.save("session", {"model": "openai:gpt-4o"})

    store.delete("session")

    assert store.load("session") == {}
    assert not store._path("session").is_file()


def test_delete_is_a_noop_when_nothing_persisted(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)

    store.delete("session")  # must not raise

    assert store.load("session") == {}


def test_sessions_are_independent(tmp_path: Path) -> None:
    """Two sessions must not cross-contaminate.

    This is the regression for the old env-var handoff (writing a single
    process-global ``BUB_MODEL``), which let concurrent sessions overwrite each
    other. Per-session files keep them isolated.
    """
    store = SessionStateStore(tmp_path)

    store.save("slack:C123", {"model": "openai:gpt-4o"})
    store.save("slack:C456", {"model": "anthropic:claude-3"})

    assert store.load("slack:C123") == {"model": "openai:gpt-4o"}
    assert store.load("slack:C456") == {"model": "anthropic:claude-3"}

    # Mutating one session must leave the other untouched.
    store.save("slack:C456", {"model": "openai:gpt-4o"})

    assert store.load("slack:C123") == {"model": "openai:gpt-4o"}
    store.delete("slack:C456")
    assert store.load("slack:C123") == {"model": "openai:gpt-4o"}


def test_corrupted_file_returns_empty(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store._path("broken").write_text("{bad json", encoding="utf-8")

    assert store.load("broken") == {}


def test_non_object_payload_returns_empty(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path)
    store._path("weird").write_text("[1, 2, 3]", encoding="utf-8")

    assert store.load("weird") == {}


def test_session_id_is_sanitised_into_safe_filename(tmp_path: Path) -> None:
    assert sanitise_session_id("slack:C123/room") == "slack_C123_room"
    store = SessionStateStore(tmp_path)

    store.save("slack:C123/room", {"model": "openai:gpt-4o"})

    assert (tmp_path / "slack_C123_room.json").is_file()
    assert store.load("slack:C123/room") == {"model": "openai:gpt-4o"}


def test_base_directory_created_only_on_write(tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested"
    store = SessionStateStore(nested)

    assert not nested.exists()  # lazy: constructing/reading does not create it

    store.save("session", {"model": "openai:gpt-4o"})

    assert nested.is_dir()
    assert store.load("session") == {"model": "openai:gpt-4o"}
