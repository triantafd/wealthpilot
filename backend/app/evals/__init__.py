"""Evaluation harness.

The pure parts — the dataset schema, the metric arithmetic, the report shaping —
live in the backend package so they are linted, type checked and unit tested
like everything else. `evals/run.py` at the repo root is the documented command
and is a thin CLI over this.

A metric nobody tests is a number you trust for no reason.
"""
