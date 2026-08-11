"""Anvitech PPC — the pure scheduling engine.

Everything under ``engine`` is pure Python: no file I/O, no web, no global mutable
state. Given the shop's masters and a chosen order sequence, it produces a concrete,
constraint-legal schedule and scores it. This purity is what makes the engine
deterministic, unit-testable, and safe to call thousands of times inside the search.
"""
