"""Per-file parse marks survive a restart, but only once their turns are stored.

Without persisted marks every cold start re-parses the whole corpus (5,120
files, over five minutes measured on the live server) to rediscover what the
store already holds. With them, an unchanged file costs one stat().

The invariant that makes this safe: a mark reaches disk only after the flusher
reports every enqueued turn WRITTEN, so a crash can never leave a mark that
outruns the data. On plain `serve` (no writer) marks are neither loaded nor
saved: nothing persists the derived turns there, so skipping a file would drop
them entirely.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import seed_history_store

import tokenol.serve.state as _state_mod
from tokenol.serve.state import ParseCache, build_snapshot_full

FIXTURES_DIR = Path(__file__).parent / "fixtures"
# basic.jsonl holds two assistant turns, both dated 2026-04-14.
BASIC_JSONL_TURNS = 2


def test_marks_round_trip_and_tolerate_garbage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path / "hist"))
    from tokenol.persistence.marks import clear_marks, load_marks, marks_path, save_marks

    assert load_marks() == {}
    marks = {tmp_path / "a.jsonl": 123, tmp_path / "b.jsonl": 456}
    save_marks(marks)
    assert marks_path().is_file()
    assert load_marks() == marks
    assert not list(marks_path().parent.glob("*.tmp")), "temp file left behind"

    marks_path().write_text("{not json")
    assert load_marks() == {}

    clear_marks()
    clear_marks()  # idempotent
    assert not marks_path().exists()


@pytest.fixture
def persist_setup(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore

    (tmp_path / "projects").mkdir(parents=True)
    src = tmp_path / "projects" / "sess-001.jsonl"
    src.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path / ".tokenol"))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True)
    seed_history_store(db, days_ago=5, turns=3)
    store = HistoryStore(db)
    store._hot_window_days = 90
    queue = FlushQueue(store, count_threshold=1_000_000, interval_seconds=1000)
    yield store, queue, src
    store.close()


@pytest.mark.asyncio
async def test_marks_are_saved_only_after_the_flush_is_written(persist_setup, monkeypatch) -> None:
    store, queue, src = persist_setup
    from tokenol.persistence.marks import marks_path

    monkeypatch.setattr(_state_mod, "_MARKS_SAVE_INTERVAL_SECONDS", 0.0)
    cache = ParseCache()

    build_snapshot_full(cache, history_store=store, flush_queue=queue)  # parses, enqueues
    assert not marks_path().exists(), "marks written before the turns were flushed"

    build_snapshot_full(cache, history_store=store, flush_queue=queue)  # still unflushed
    assert not marks_path().exists()

    await queue._drain_once()
    assert queue.all_written()
    build_snapshot_full(cache, history_store=store, flush_queue=queue)  # first tick after the write
    assert marks_path().is_file()
    saved = json.loads(marks_path().read_text())
    assert saved == {str(src): src.stat().st_mtime_ns}


@pytest.mark.asyncio
async def test_a_restart_skips_files_whose_marks_are_persisted(persist_setup, monkeypatch) -> None:
    store, queue, src = persist_setup
    monkeypatch.setattr(_state_mod, "_MARKS_SAVE_INTERVAL_SECONDS", 0.0)
    first = ParseCache()
    build_snapshot_full(first, history_store=store, flush_queue=queue)
    await queue._drain_once()
    build_snapshot_full(first, history_store=store, flush_queue=queue)  # saves marks

    parsed: list[Path] = []
    real = _state_mod.parse_file

    def spy(path):
        parsed.append(path)
        return real(path)

    monkeypatch.setattr(_state_mod, "parse_file", spy)

    # "Restart": a fresh ParseCache against the same store and sidecar.
    second = ParseCache()
    result = build_snapshot_full(second, history_store=store, flush_queue=queue)

    assert parsed == [], f"unchanged files were re-parsed after restart: {parsed}"
    # basic.jsonl is dated 2026-04-14, older than the 90-day hot window, so its
    # turns live in the WARM tier after a restart, not in result.turns (hot
    # only). Prove they are in the store and known to the dedup set instead.
    stored = {k for k in store.dedup_keys() if not k.startswith("warm-")}
    assert len(stored) == BASIC_JSONL_TURNS, "the skipped file's turns must be in the store"
    assert stored <= second._known_dedup_keys
    assert not any(t.session_id == "sess-001" for t in result.turns), "old turns must not be rebuilt into the hot tier"

    # A file that changed while the server was down IS parsed again.
    time.sleep(0.01)
    src.write_bytes(src.read_bytes() + b"\n")
    build_snapshot_full(second, history_store=store, flush_queue=queue)
    assert parsed == [src]


@pytest.mark.asyncio
async def test_save_cadence_is_bounded(persist_setup, monkeypatch) -> None:
    """First eligible tick saves; a change inside the interval waits its turn.

    The "never saved yet" sentinel must NOT be a clock reading. It was 0.0, and
    time.monotonic()'s origin is arbitrary — uptime, on Linux — so on a
    long-running machine 0.0 read as "never" and the first tick saved, while on
    a freshly booted CI runner it read as "saved just now" and nothing saved at
    all. That is why this test went red on 3.10/3.11/3.12 and green here. The
    `is None` assertion below is the regression pin; the rest of the test then
    drives the interval by moving the recorded time, never the clock.
    """
    store, queue, src = persist_setup
    from tokenol.persistence import marks as marks_mod

    saves: list[dict] = []
    monkeypatch.setattr(_state_mod, "save_marks", lambda m: saves.append(dict(m)))
    monkeypatch.setattr(_state_mod, "_MARKS_SAVE_INTERVAL_SECONDS", 1000.0)

    cache = ParseCache()
    build_snapshot_full(cache, history_store=store, flush_queue=queue)
    assert cache._marks_saved_at is None, "'never saved' must not be expressed as a clock value"
    assert saves == [], "nothing is written before the flush"

    await queue._drain_once()
    build_snapshot_full(cache, history_store=store, flush_queue=queue)
    assert len(saves) == 1, "the first eligible tick must save whatever the machine's uptime"
    assert saves[0] == {src: src.stat().st_mtime_ns}
    assert cache._marks_saved_at is not None

    # Change the file again, still inside the interval: must NOT save.
    time.sleep(0.01)
    src.touch()
    build_snapshot_full(cache, history_store=store, flush_queue=queue)
    await queue._drain_once()
    build_snapshot_full(cache, history_store=store, flush_queue=queue)
    assert len(saves) == 1, f"a change inside the interval must not trigger a save, got {len(saves)}"

    # Once the interval has elapsed, the pending change is written.
    cache._marks_saved_at -= 2 * _state_mod._MARKS_SAVE_INTERVAL_SECONDS
    build_snapshot_full(cache, history_store=store, flush_queue=queue)
    assert saves[1:] == [{src: src.stat().st_mtime_ns}], f"a change after the interval must save exactly once more, got {len(saves)} saves"
    assert marks_mod.marks_path().exists() is False, "save was stubbed; nothing should touch disk here"


def test_plain_serve_never_loads_or_saves_marks(persist_setup, monkeypatch) -> None:
    """No flush queue means no writer, so marks are inert even if a stale file exists."""
    store, _queue, src = persist_setup
    from tokenol.persistence.marks import marks_path, save_marks

    save_marks({src: src.stat().st_mtime_ns})  # a mark left by an earlier --persist run
    before = marks_path().stat().st_mtime_ns
    parsed: list[Path] = []
    real = _state_mod.parse_file

    def spy(path):
        parsed.append(path)
        return real(path)

    monkeypatch.setattr(_state_mod, "parse_file", spy)

    build_snapshot_full(ParseCache(), history_store=store, flush_queue=None)

    assert parsed == [src], "plain serve must parse the file despite the stale mark"
    assert marks_path().stat().st_mtime_ns == before, "plain serve must not touch the marks file"


@pytest.mark.asyncio
async def test_forget_all_clears_the_marks(tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    from datetime import datetime, timezone

    from httpx import ASGITransport, AsyncClient

    from tokenol.persistence.forget_handoff import ForgetRequest, submit_forget_request
    from tokenol.persistence.marks import marks_path, save_marks
    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    src = tmp_path / "projects" / "sess-001.jsonl"
    src.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path / ".tokenol"))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    (tmp_path / ".tokenol").mkdir(parents=True, exist_ok=True)
    save_marks({src: src.stat().st_mtime_ns})

    app = create_app(ServerConfig(persist=True), prefs_path=tmp_path / "prefs.json")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.get("/api/breakdown/summary?range=all")
        assert app.state.parse_cache._last_mtime_ns_by_path, "marks were not loaded"
        submit_forget_request(ForgetRequest(kind="all", value=None, submitted_at=datetime.now(tz=timezone.utc)))
        await app.state.broadcaster.process_pending_forget()

    assert app.state.parse_cache._last_mtime_ns_by_path == {}
    assert not marks_path().exists(), "forget-all left marks that would skip every file on the next start"


@pytest.mark.asyncio
async def test_marks_are_distrusted_when_the_store_is_empty(persist_setup, monkeypatch) -> None:
    """A store deleted out from under its marks must not silence every file.

    Marks assert "this file's turns are already in the store". If the store
    goes away (deleted by hand, or replaced with a fresh one) while the sidecar
    survives, honouring the marks would skip every transcript and serve an
    empty dashboard for ever, with nothing in the logs. An empty store plus
    non-empty marks is that contradiction, so the marks lose.
    """
    store, queue, src = persist_setup
    from tokenol.persistence.marks import marks_path, save_marks

    monkeypatch.setattr(_state_mod, "_MARKS_SAVE_INTERVAL_SECONDS", 0.0)
    first = ParseCache()
    build_snapshot_full(first, history_store=store, flush_queue=queue)
    await queue._drain_once()
    build_snapshot_full(first, history_store=store, flush_queue=queue)
    assert marks_path().is_file()
    saved_marks = json.loads(marks_path().read_text())
    assert saved_marks, "precondition: marks were written"

    # The store loses every row, but the sidecar survives.
    store.forget(all=True)
    assert store.dedup_keys() == set()
    save_marks({src: src.stat().st_mtime_ns})

    parsed: list[Path] = []
    real = _state_mod.parse_file

    def spy(path):
        parsed.append(path)
        return real(path)

    monkeypatch.setattr(_state_mod, "parse_file", spy)

    second = ParseCache()
    build_snapshot_full(second, history_store=store, flush_queue=queue)

    assert parsed == [src], "an empty store must re-read every file despite its marks"
    assert len(second._hot_turns) >= BASIC_JSONL_TURNS, "the re-read turns were not recovered"
