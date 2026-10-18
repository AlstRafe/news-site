"""Intended differences from the frozen v3 extractor, as input transformations.

The new extractor must equal v3 applied to ``intended_input(html)``. Each entry is a decision:
- O-03 (Phase 8): images inside ``<template>`` are never rendered, so they are not article
  images. Expressed as "v3 on the same page with those <img> tags removed". An unclosed
  <template> (truncated file) runs to the end of the input, as an HTML parser treats it.
"""
import re

_TEMPLATE = re.compile(rb"<template\b.*?(?:</template\s*>|\Z)", re.S | re.I)
_IMG = re.compile(rb"<img\b[^>]*>", re.I)


def intended_input(html):
    if isinstance(html, str):
        return intended_input(html.encode("utf-8")).decode("utf-8")
    return _TEMPLATE.sub(lambda m: _IMG.sub(b"", m.group(0)), html)
