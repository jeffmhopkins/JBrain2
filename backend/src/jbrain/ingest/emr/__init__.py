"""EMR / medical-record import (docs/plans/EMR_IMPORT_PLAN.md).

Decrypts an owner-filed encrypted ZIP of EMR exports, parses the records with
deterministic per-source parsers, and lowers them into the shipped entities/facts
graph as cited, health-domain-firewalled measurement (labs) and event (admissions)
facts — surfaced via the lab_results/encounters projections and the
read_labs/read_encounters tools.
"""
