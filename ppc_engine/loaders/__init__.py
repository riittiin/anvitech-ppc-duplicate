"""Loaders — read the uploaded Excel workbook into pure domain objects.

The workbook is READ-ONLY (RULES.md). Loading is *tolerant and fail-localized*
(LESSONS.md): quirks (trailing spaces in sheet names, an apostrophe in a name,
``CNC 5`` vs ``CNC5``) are normalised, and data gaps (an item with no routing, a
routing referencing a machine not yet in the master) are collected into a
non-blocking DataReport rather than crashing the load.

Public entry point: ``load_all(path) -> LoadResult``.
"""

from ppc_engine.loaders.loader import LoadResult, load_all
from ppc_engine.loaders.report import DataGap, DataReport, GapKind

__all__ = ["load_all", "LoadResult", "DataReport", "DataGap", "GapKind"]
