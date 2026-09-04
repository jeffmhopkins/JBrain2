"""The api and the sidecar must agree on what the radio can tune.

They cannot share the constant: `deploy/sdr/` ships in its own container and imports
nothing from the backend. So it is duplicated on purpose, and this is the part that
cannot be fixed by sharing a module — a check that the duplicate has not drifted.

It HAD drifted, which is why this exists. `api/sdr.py` and `agent/sdrtools.py` both said
0.024 MHz against the sidecar's 24. A request for anything between passed every bound
the api enforces and came back a 502 from the radio: not a wrong answer but a right one
from the wrong layer, with an error message to match. Two copies out of three were wrong
and nothing noticed, because nothing compared them.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from jbrain.sdr.tuner import DIRECT_MIN_MHZ, MAX_MHZ, MIN_MHZ, serials_in

_SIDECAR = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "sdr" / "listen.py"


def _constant(name: str) -> int:
    """Read one `NAME = 123_456` off the sidecar's source.

    By text, not by import: `deploy/sdr/` modules import each other by bare name because
    they share one WORKDIR in the image, so importing this one from here fails on its
    siblings rather than on anything real."""
    found = re.search(rf"^{name} = ([0-9_]+)$", _SIDECAR.read_text(), re.MULTILINE)
    assert found, f"{name} is not in {_SIDECAR} — did it move?"
    return int(found.group(1).replace("_", ""))


@pytest.mark.skipif(not _SIDECAR.exists(), reason="the sidecar is not in this checkout")
def test_the_api_refuses_exactly_what_the_sidecar_refuses() -> None:
    assert _constant("MIN_HZ") == MIN_MHZ * 1_000_000
    assert _constant("MAX_HZ") == MAX_MHZ * 1_000_000
    # The SECOND range, added when HF landed. It is the same class of duplicate and the
    # same class of silent failure: a sidecar that disagreed here would refuse a
    # shortwave frequency the api had already accepted, as a 502 rather than a bound.
    assert _constant("DIRECT_MIN_HZ") == DIRECT_MIN_MHZ * 1_000_000


def test_no_source_file_writes_the_tuner_range_as_a_literal() -> None:
    """The bound has to be imported, not retyped — which is the only durable version of
    the fix, since the value being wrong was never the point.

    It was retyped FOUR times: `api/sdr.py`, `agent/sdrtools.py`, and twice more as bare
    `Query(gt=0.024, ...)` literals inside `api/debug.py`, found only after the first
    three were consolidated and I had already said the job was done. Sharing a module
    fixes the copies that exist; this fixes the next one."""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "jbrain"
    offenders = [
        f"{path.relative_to(root)}:{n}"
        for path in root.rglob("*.py")
        if path.name != "tuner.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"(?<![\w.])0\.024(?![\w])|(?<![\w.])1766\.0(?![\w])", line)
    ]

    assert offenders == [], f"tuner range hardcoded instead of imported: {offenders}"


class TestReadingTheUsbScan:
    """`serials_in` parses an external payload and decides which radios exist.

    It had no test at all, and its docstring claimed one — the sort of claim that is
    only ever checked by someone going to look. The load-bearing part is that it
    distinguishes "the scan saw nothing" from "the scan could not see": collapsing them
    made a healthy scan reporting zero radios skip the refusal entirely, so a service
    dedicated to an absent dongle started on whatever enumerated first.
    """

    def test_it_reads_the_serials_the_scan_found_IN_ORDER(self) -> None:
        """Sorted, so "the first one" is stable across reboots rather than USB order —
        which is the entire bug this feature removes.

        Enough serials that set iteration does not land on sorted order by luck: with
        two it does, and a version returning `list(set(...))` passed."""
        serials = [
            "A1B2C3D4",
            "77192819",
            "99887766",
            "41550903",
            "09022796",
            "55443322",
            "12345678",
            "ZZ001122",
        ]
        found = serials_in({"sysfs_readable": True, "sdrs": [{"serial": s} for s in serials]})

        assert found == sorted(serials)

    def test_a_scan_that_saw_nothing_is_an_empty_list_not_a_blind_one(self) -> None:
        assert serials_in({"sysfs_readable": True, "sdrs": []}) == []

    def test_a_scan_that_could_not_see_is_None(self) -> None:
        # A supervisor with no /sys mounted answers 200 with sysfs_readable false. That
        # is not "no radios", and treating it as such is how the refusal gets skipped.
        assert serials_in({"sysfs_readable": False, "sdrs": []}) is None
        assert serials_in({"sdrs": [{"serial": "09022796"}]}) is None
        assert serials_in("not a payload") is None
        assert serials_in(None) is None

    def test_a_device_with_no_serial_is_left_out(self) -> None:
        # It cannot be named to `-d`, so listing it would offer a selection that cannot
        # actually be made.
        found = serials_in(
            {"sysfs_readable": True, "sdrs": [{"serial": ""}, {}, {"serial": 5}, {"serial": "x1"}]}
        )

        assert found == ["x1"]

    def test_the_same_radio_twice_is_one_radio(self) -> None:
        payload = {"sysfs_readable": True, "sdrs": [{"serial": "x1"}, {"serial": "x1"}]}

        assert serials_in(payload) == ["x1"]
