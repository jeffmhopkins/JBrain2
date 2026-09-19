---
name: run_python
version: 1
permission: web
params:
  type: object
  properties:
    code:
      type: string
      description: >-
        The Python to run. Write it as you would in a notebook: statements, then a bare
        expression on the last line whose value you want back. Use print() for anything you
        want to see along the way.
    timeout_seconds:
      type: number
      description: >-
        How long the code may run. Defaults to 10; anything above 60 is clamped. Raise it
        only for work you expect to be slow — a timeout usually means a loop that never ends,
        not a computation that needed longer.
  required: [code]
examples:
  - {code: "from statistics import median, mean\nvalues = [12, 7, 3, 19, 4, 8]\nprint(\"mean\", mean(values))\nmedian(values)"}
  - {code: "from datetime import date\n(date(2026, 12, 25) - date(2026, 9, 18)).days"}
  - {code: "rows = [(\"rent\", 1450), (\"power\", 98), (\"water\", 41)]\ntotal = sum(amount for _, amount in rows)\nfor name, amount in rows:\n    print(f\"{name:8} {amount:6} {amount / total:6.1%}\")\ntotal"}
---
Run a short Python program and get back what it printed and what it evaluated to. This is
for the computation `calculate` cannot express as one expression: a loop, statistics over a
set of numbers, date and duration arithmetic, a unit-conversion chain, a running total, or
checking a figure against a table of numbers you are holding.

The last line is the answer. End with a bare expression and its value comes back as
`result` — `median(values)`, `total`, `(end - start).days`. Anything you print along the way
comes back as `stdout`, so print the intermediate steps you want the owner to be able to
check. If the code ends in a statement there is simply no final value, which is fine when
you printed what mattered.

It is a sealed calculator. The standard library only — no numpy, no pandas, no requests —
and **no network, no files, and no other programs**: `import requests`, `open("/etc/passwd")`
and `os.system(...)` are all refused, immediately and by name. It cannot read the owner's
notes, the web, or anything else; the only data it has is the data you type into the
snippet. So bring the numbers with you — write them into the code as literals — and never
assume it can go and look something up.

Each call is a fresh process with nothing carried over: no variables, no files, no imports
from the call before. Write each snippet to stand on its own. It is stopped after 10 seconds
(raise `timeout_seconds` up to 60 if you genuinely need longer), and long output is cut
short with a note saying so.

When it fails you get one line — the exception and the line number — which is enough to fix
the snippet and try again. Do that rather than falling back to working the answer out in your
head; two tool calls are cheaper than one wrong number.

Say what you are about to compute before you call it — which numbers, which formula — then
call it and report what it returned. For a single expression, `calculate` is the better tool:
it is faster and gives exact fractions and symbolic forms where this gives floats.
