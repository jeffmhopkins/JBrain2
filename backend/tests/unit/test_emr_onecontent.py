"""The OneContent geometry parser (docs/plans/EMR_IMPORT_PLAN.md §6.2/§6.3).

Two things are proven: (1) the §6.2 go/no-go — x-geometry band slicing recovers
columns (incl. a multi-word analyte name) from the real `get_text("words")` view
while a character-offset ruler over the reflowed reading-order text misaligns; and
(2) the full parser — account-keyed grouping into ambulatory encounters, the
abnormal-flag legend → interpretation, and each row's `Collected` timestamp as the
draw's `valid_from`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jbrain.ingest.emr import onecontent
from jbrain.ingest.emr.onecontent import WordBox, parse_onecontent

_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "emr"
_WORDS = json.loads((_DIR / "onecontent_words.json").read_text())
_TEXT = (_DIR / "onecontent_account.txt").read_text()


def _words() -> list[WordBox]:
    return [WordBox(w[0], w[1], w[2], w[3], w[4]) for w in _WORDS["words"]]


# --- §6.2 go/no-go: geometry recovers columns, char-offset does not -------


def test_geometry_recovers_columns_and_joins_multiword_analyte() -> None:
    lines = onecontent._group_lines(_words())
    bands = onecontent._column_bands(lines[0])
    assert bands is not None
    rows = [onecontent._slice_row(line, bands) for line in lines[1:]]

    plt, hgb, wbc = rows
    assert plt["analyte"] == "Platelet count" and plt["result"] == "38" and plt["flag"] == "L*"
    assert plt["units"] == "10*3/uL" and plt["ref"] == "150-400"
    assert plt["collected"] == "07/22/2021 07:12"  # date + time share the band, joined
    assert hgb["analyte"] == "Hemoglobin" and hgb["result"] == "10.9"
    # The multi-word analyte lands whole in the analyte band — the char-ruler's nemesis.
    assert wbc["analyte"] == "White Blood Cell Count" and wbc["result"] == "3.2"


def test_char_offset_ruler_misaligns_on_reflowed_text() -> None:
    # Reading-order text collapses inter-column runs of whitespace, so slicing a data
    # row at the header word's CHARACTER offset reads into the wrong column. This is
    # why OneContent needs geometry (§6.2) — the negative control for the test above.
    lines = onecontent._group_lines(_words())
    header_text = onecontent._line_text(lines[0])
    row_text = onecontent._line_text(lines[1])  # "Platelet count 38 L* 10*3/uL ..."
    result_off = header_text.index("Result")
    # At the header's "Result" offset the data row is still inside "Platelet count",
    # never the value 38 — a char ruler cannot recover the column.
    assert not row_text[result_off:].startswith("38")


# --- full parser: account grouping + legend + per-row dates ---------------
#
# The full corpus (4 accounts, per-row dates) is exercised on column-aligned geometry
# built directly — value tokens sit UNDER their header band, as a real page prints
# (the reflow misalignment is the real words fixture's job, above). Column x-bands
# mirror the real `onecontent_words.json` (analyte~40 … collected~520).

_COLS = {"analyte": 40.0, "result": 240.0, "flag": 292.0, "units": 332.0, "ref": 412.0}
_COLLECTED_X = 520.0


def _box(x: float, y: float, word: str) -> WordBox:
    return WordBox(x, y, x + len(word) * 6.0, y + 10.0, word)


def _header(y: float) -> list[WordBox]:
    labels = [
        ("Analyte", 40.0),
        ("Result", 240.0),
        ("Ab", 292.0),
        ("Units", 332.0),
        ("Ref", 412.0),
        ("Range", 452.0),
        ("Collected", 520.0),
    ]
    return [_box(x, y, label) for label, x in labels]


def _row(
    y: float, analyte: str, result: str, flag: str, units: str, ref: str, ts: str
) -> list[WordBox]:
    boxes: list[WordBox] = []
    x = _COLS["analyte"]
    for tok in analyte.split():  # multi-word analyte shares the analyte band
        boxes.append(_box(x, y, tok))
        x += len(tok) * 6.0 + 4.0
    for name, val in (("result", result), ("flag", flag), ("units", units), ("ref", ref)):
        if val:
            boxes.append(_box(_COLS[name], y, val))
    day, clock = ts.split()  # date + time are two words in the collected band
    boxes += [_box(_COLLECTED_X, y, day), _box(_COLLECTED_X + 70.0, y, clock)]
    return boxes


def _account_page(*blocks: tuple[str, list[list[WordBox]]]) -> list[WordBox]:
    """One page of account blocks: each an `Account:` line + header + data rows."""
    page: list[WordBox] = []
    y = 20.0
    for acct, rows in blocks:
        page += [_box(40.0, y, "Account:"), _box(120.0, y, acct)]
        y += 18.0
        page += _header(y)
        y += 18.0
        for row in rows:
            page += [WordBox(b.x0, y, b.x1, b.y1, b.text) for b in row]
            y += 18.0
        y += 12.0
    return page


def _corpus() -> list[list[WordBox]]:
    p1 = _account_page(
        (
            "C0000202101",
            [
                _row(0, "Platelet count", "38", "L*", "10*3/uL", "150-400", "07/22/2021 07:12"),
                _row(0, "Hemoglobin", "10.9", "L", "g/dL", "13.5-17.5", "07/22/2021 07:12"),
                _row(
                    0,
                    "White Blood Cell Count",
                    "3.2",
                    "L",
                    "10*3/uL",
                    "4.0-11.0",
                    "07/22/2021 07:12",
                ),
            ],
        ),
        (
            "C0000202001",
            [
                _row(0, "Platelet count", "205", "", "10*3/uL", "150-400", "11/03/2020 10:48"),
                _row(0, "Hemoglobin", "14.6", "", "g/dL", "13.5-17.5", "11/03/2020 10:48"),
                _row(0, "Potassium", "4.3", "", "mmol/L", "3.5-5.1", "11/03/2020 10:48"),
            ],
        ),
    )
    p2 = _account_page(
        (
            "C0000202201",
            [
                _row(0, "Platelet count", "171", "", "10*3/uL", "150-400", "05/19/2022 14:03"),
                _row(
                    0,
                    "White Blood Cell Count",
                    "7.1",
                    "",
                    "10*3/uL",
                    "4.0-11.0",
                    "05/19/2022 14:03",
                ),
            ],
        ),
        (
            "C0000202501",
            [
                _row(0, "Platelet count", "120", "L", "10*3/uL", "150-400", "09/30/2025 06:55"),
                _row(0, "Potassium", "5.0", "", "mmol/L", "3.5-5.1", "09/30/2025 06:55"),
            ],
        ),
    )
    return [p1, p2]


def test_fingerprint_matches_onecontent() -> None:
    assert onecontent.fingerprint(_TEXT)
    assert not onecontent.fingerprint("just some prose without a lab legend")


def test_full_parse_groups_by_account_with_dates_and_flags() -> None:
    result = parse_onecontent(_corpus(), legend_text=_TEXT)
    encounters = {e.key: e for e in result.encounters}
    # Four account blocks across two pages -> four ambulatory lab visits.
    assert set(encounters) == {"C0000202101", "C0000202001", "C0000202201", "C0000202501"}
    assert all(e.encounter_class == "ambulatory" for e in result.encounters)
    assert all(e.source_system == "onecontent" for e in result.encounters)

    e2021 = encounters["C0000202101"]
    assert {o.analyte.name for o in e2021.observations} >= {
        "Platelet count",
        "Hemoglobin",
        "White blood cell count",
    }
    plt = next(o for o in e2021.observations if o.analyte.name == "Platelet count")
    assert plt.value_num == 38.0
    assert plt.collected_at == datetime(2021, 7, 22, 7, 12, tzinfo=UTC)
    assert plt.collected_at == e2021.admitted_at  # the account visit spans its draws
    assert plt.interpretation == "critical"  # "L*" -> critical wins over low
    assert plt.ref_low == 150.0 and plt.ref_high == 400.0
    assert plt.specimen_id == "" and plt.fhir_status == "final"

    # A normal (unflagged) reading carries no interpretation.
    pot = next(o for o in encounters["C0000202001"].observations if o.analyte.name == "Potassium")
    assert pot.interpretation is None
    assert pot.collected_at == datetime(2020, 11, 3, 10, 48, tzinfo=UTC)


def test_wbc_canonicalizes_across_accounts() -> None:
    # The load-bearing dedup key: "White Blood Cell Count" -> one canonical code (§6.3).
    result = parse_onecontent(_corpus(), legend_text=_TEXT)
    wbcs = [
        o
        for e in result.encounters
        for o in e.observations
        if o.analyte.name == "White blood cell count"
    ]
    assert wbcs and {o.analyte.code for o in wbcs} == {"6690-2"}
