"""Parser unit tests."""

from pathlib import Path

from tokenol.enums import AssumptionTag
from tokenol.ingest.parser import iter_assistant_events, parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_basic():
    events = list(parse_file(FIXTURES / "basic.jsonl"))
    assert len(events) == 2
    assert all(e.event_type == "assistant" for e in events)
    assert events[0].model == "claude-opus-4-7"
    assert events[0].usage is not None
    assert events[0].usage.input_tokens == 1000
    assert events[0].usage.output_tokens == 200


def test_parse_captures_cwd_from_any_event_type(tmp_path):
    """Session label should come from the earliest cwd seen — even if it's on a
    user/assistant event — because Claude Code can silently change cwd mid-session
    (e.g. when a task implicitly scopes to a subdir). First-recorded cwd is
    the launch dir, which matches the user's mental model."""
    p = tmp_path / "cwd_drift.jsonl"
    p.write_text(
        '{"type":"user","timestamp":"2026-04-14T10:00:00Z","sessionId":"s1","cwd":"/repo","message":{"role":"user","content":"hi"}}\n'
        '{"type":"system","timestamp":"2026-04-14T10:05:00Z","sessionId":"s1","cwd":"/repo/sub","subtype":"bg"}\n'
    )
    events = list(parse_file(p))
    assert events[0].cwd == "/repo"
    assert events[1].cwd == "/repo/sub"


def test_parse_interrupted():
    events = list(parse_file(FIXTURES / "interrupted.jsonl"))
    assert len(events) == 1
    assert events[0].usage is None
    assert events[0].stop_reason is None


def test_parse_sidechain():
    events = list(parse_file(FIXTURES / "sidechain.jsonl"))
    assert len(events) == 1
    assert events[0].is_sidechain is True


def test_dedup_removes_duplicate():
    """Two events with same message.id + requestId should yield only one."""
    deduplicated = list(iter_assistant_events([FIXTURES / "dedup.jsonl"]))
    assert len(deduplicated) == 1


def test_dedup_passthrough_on_missing_ids():
    """Events missing message.id or requestId pass through without dedup."""
    results = list(iter_assistant_events([FIXTURES / "missing_ids.jsonl"]))
    assert len(results) == 1
    ev, tags = results[0]
    assert AssumptionTag.DEDUP_PASSTHROUGH in tags


def test_interrupted_turn_tag():
    """Interrupted turns get INTERRUPTED_TURN_SKIPPED tag."""
    results = list(iter_assistant_events([FIXTURES / "interrupted.jsonl"]))
    assert len(results) == 1
    ev, tags = results[0]
    assert AssumptionTag.INTERRUPTED_TURN_SKIPPED in tags


def test_parse_normalizes_windows_cwd(tmp_path):
    """Windows drive-letter and UNC paths are normalized to POSIX separators at ingestion."""
    import json
    p = tmp_path / "sess.jsonl"
    lines = [
        # Drive-letter path with backslashes
        json.dumps({
            "type": "system", "timestamp": "2026-04-14T10:00:00Z", "sessionId": "s1",
            "uuid": "u1", "isSidechain": False, "cwd": r"C:\Users\alice\dev\proj",
        }),
        json.dumps({
            "type": "assistant", "timestamp": "2026-04-14T10:00:00Z", "sessionId": "s1",
            "requestId": "r1", "uuid": "e1", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {"id": "m1", "role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        }),
        # UNC path
        json.dumps({
            "type": "system", "timestamp": "2026-04-14T10:00:00Z", "sessionId": "s2",
            "uuid": "u2", "isSidechain": False, "cwd": r"\\fileserver\share\work",
        }),
        json.dumps({
            "type": "assistant", "timestamp": "2026-04-14T10:00:00Z", "sessionId": "s2",
            "requestId": "r2", "uuid": "e2", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {"id": "m2", "role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        }),
        # POSIX path — untouched
        json.dumps({
            "type": "system", "timestamp": "2026-04-14T10:00:00Z", "sessionId": "s3",
            "uuid": "u3", "isSidechain": False, "cwd": "/home/alice/dev/proj",
        }),
        json.dumps({
            "type": "assistant", "timestamp": "2026-04-14T10:00:00Z", "sessionId": "s3",
            "requestId": "r3", "uuid": "e3", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {"id": "m3", "role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        }),
    ]
    p.write_text("\n".join(lines) + "\n")

    events = list(parse_file(p))
    by_sid = {e.session_id: e for e in events if e.cwd}
    assert by_sid["s1"].cwd == "C:/Users/alice/dev/proj"
    assert by_sid["s2"].cwd == "//fileserver/share/work"
    assert by_sid["s3"].cwd == "/home/alice/dev/proj"
