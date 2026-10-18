"""Route registration without blueprint prefixes.

Endpoint names ("index", "show_article", "translation_settings", ...) are part of the
templates' contract and predate the split into modules; a Blueprint would rename them
to "pages.index" etc. Each view module collects its routes here and ``create_app``
registers them under their plain names.
"""
from __future__ import annotations

from flask import Flask


class Routes:
    def __init__(self) -> None:
        self._rules: list[tuple[str, str, object, tuple[str, ...]]] = []

    def route(self, rule: str, endpoint: str, methods: tuple[str, ...] = ("GET",)):
        def decorator(view):
            self._rules.append((rule, endpoint, view, methods))
            return view
        return decorator

    def get(self, rule: str, endpoint: str):
        return self.route(rule, endpoint, ("GET",))

    def post(self, rule: str, endpoint: str):
        return self.route(rule, endpoint, ("POST",))

    def register(self, app: Flask) -> None:
        for rule, endpoint, view, methods in self._rules:
            app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=list(methods))
