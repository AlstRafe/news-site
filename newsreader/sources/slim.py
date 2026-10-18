"""Make a saved page smaller without changing what the reader extracts from it (§9, D-11).

Removes what a saved news page never needs to be read here: scripts (except JSON-LD, which
carries the publication time), styles, inline SVG, iframes, ``<template>``, media players,
comments, resource hints and event-handler attributes. Text, links, images, ``<meta>`` and the
canonical link stay.

``slim_html`` is pure. Callers must keep the original unless ``extract_article`` gives the
same result for both (``slim_if_equivalent`` does exactly that) — the extraction-equality
check is what makes slimming safe, not the list of removed tags.
"""
from __future__ import annotations

from bs4 import BeautifulSoup, Comment

from newsreader.sources import ReaderError, extract_article

DROP_TAGS = ("script", "style", "svg", "noscript", "iframe", "template", "canvas", "video", "audio")
DROP_REL = {"stylesheet", "preload", "prefetch", "modulepreload", "preconnect", "dns-prefetch", "icon",
            "apple-touch-icon", "manifest", "mask-icon", "shortcut icon", "alternate"}
DROP_ATTRS = ("style", "onclick", "onload", "onerror")


def slim_html(html: bytes) -> bytes:
    """The slimmed page as UTF-8 (a ``<meta charset>`` is rewritten to match)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(DROP_TAGS):
        if tag.name == "script" and "ld+json" in str(tag.get("type", "")).lower():
            continue
        tag.decompose()
    for link in soup.find_all("link"):
        if link.attrs is None:
            continue
        if {str(rel).lower() for rel in (link.get("rel") or [])} & DROP_REL:
            link.decompose()
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in soup.find_all(True):
        if tag.attrs is None:
            continue
        for attribute in DROP_ATTRS:
            tag.attrs.pop(attribute, None)
    # encode() (not str()) so that bs4 rewrites <meta charset=...> / http-equiv to UTF-8.
    return soup.encode("utf-8")


def slim_if_equivalent(html: bytes, extracted: dict | None = None) -> tuple[bytes, bool]:
    """(bytes to keep, slimmed?). The slim version is used only if it is smaller and extracts
    exactly the same article; otherwise the original comes back unchanged."""
    try:
        before = extracted if extracted is not None else extract_article(html)
        slim = slim_html(html)
        if len(slim) >= len(html) or extract_article(slim) != before:
            return html, False
    except (ReaderError, ValueError, RecursionError):
        return html, False
    return slim, True
