"""The in-memory debug-activity ring + route classifier (api/debug_activity)."""

from jbrain.api.debug_activity import DebugActivity, classify


def test_classify_maps_each_route_to_a_label() -> None:
    assert classify("GET", "/api/debug/whoami") == "whoami"
    assert classify("POST", "/api/debug/complete") == "complete"
    assert classify("POST", "/api/debug/sql") == "sql"
    assert classify("GET", "/api/debug/logs/api") == "logs"
    assert classify("GET", "/api/debug/llm") == "routing"
    assert classify("PUT", "/api/debug/llm") == "switch"
    assert classify("POST", "/api/debug/llm/local-models/x/load") == "load"
    assert classify("POST", "/api/debug/llm/local-models/x/unload") == "unload"
    assert classify("POST", "/api/debug/suspend-self") == "suspend"
    assert classify("POST", "/api/debug/revoke-self") == "revoke"


def test_ring_records_and_snapshots_incrementally() -> None:
    a = DebugActivity(maxlen=3)
    a.record(method="GET", path="/api/debug/whoami", status=200, client="c1")
    a.record(method="POST", path="/api/debug/sql", status=200, client="", detail="select 1")

    snap = a.snapshot()
    assert [e["seq"] for e in snap["events"]] == [1, 2]
    assert snap["last"] == 2
    assert snap["events"][0]["kind"] == "whoami" and snap["events"][0]["client"] == "c1"
    # The command detail (SQL/prompt) rides along so the console shows what ran.
    assert snap["events"][1]["detail"] == "select 1" and snap["events"][0]["detail"] == ""

    # `after` returns only newer events — the console's incremental poll.
    assert [e["seq"] for e in a.snapshot(after=1)["events"]] == [2]

    # The ring is bounded (maxlen 3): a third/fourth record drops the oldest while
    # seq keeps rising, so the snapshot holds 2,3,4 and the high-water is 4.
    a.record(method="GET", path="/api/debug/llm", status=200, client="")
    a.record(method="PUT", path="/api/debug/llm", status=200, client="")
    assert [e["seq"] for e in a.snapshot()["events"]] == [2, 3, 4]
    assert a.snapshot()["last"] == 4


def test_detail_is_truncated() -> None:
    a = DebugActivity()
    a.record(method="POST", path="/api/debug/sql", status=200, client="", detail="x" * 500)
    detail = a.snapshot()["events"][0]["detail"]
    assert len(detail) == 301 and detail.endswith("…")
