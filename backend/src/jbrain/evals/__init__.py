"""Eval corpora + scorers shipped IN the `jbrain` package.

The runtime-needed pieces — the case fixtures and the scoring cores — live here, inside
the installed package, so a scheduled `eval_run` can score the live model in PRODUCTION
(the container image ships `src/jbrain`). The owner-run box calibration CLI
(`backend/evals/box/`) imports its scorers from here.

R4 deleted the `note.extract` and `integrate.note` corpora with the prompts they scored.
What is left scores `entity.disambiguate` and the wiki lint; the reading that replaced
the deleted two has no committed corpus yet (R5, beside the wipe).
"""
