"""Per-job log capture: a LogScope taps the structured-log events emitted inside
it into a bounded, JSON-safe buffer; outside a scope nothing is captured."""

import uuid

import structlog

from jbrain.log_capture import _MAX_EVENTS, LogScope, configure_logging


def test_logscope_captures_events_emitted_inside_it() -> None:
    configure_logging()
    log = structlog.get_logger()
    with LogScope() as scope:
        log.info("did.a.thing", n=3, who="me")
        log.warning("uh.oh", reason="boom")
    names = [e["event"] for e in scope.events]
    assert "did.a.thing" in names and "uh.oh" in names
    thing = next(e for e in scope.events if e["event"] == "did.a.thing")
    assert thing["n"] == 3 and thing["who"] == "me"
    assert "timestamp" in thing  # TimeStamper ran before the capture tap


def test_no_capture_outside_a_scope() -> None:
    configure_logging()
    log = structlog.get_logger()
    log.info("orphan.event")  # no active scope: must not raise, nothing captured
    with LogScope() as scope:
        pass
    assert scope.events == []


def test_non_primitive_values_are_stringified() -> None:
    configure_logging()
    log = structlog.get_logger()
    uid = uuid.uuid4()
    with LogScope() as scope:
        log.info("has.uuid", id=uid)
    ev = next(e for e in scope.events if e["event"] == "has.uuid")
    assert ev["id"] == str(uid)  # coerced JSON-safe for the JSONB column


def test_capture_is_bounded() -> None:
    configure_logging()
    log = structlog.get_logger()
    with LogScope() as scope:
        for i in range(_MAX_EVENTS + 25):
            log.info("flood", i=i)
    assert len(scope.events) == _MAX_EVENTS
