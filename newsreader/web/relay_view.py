"""The translation relay a NewsBridge offers to other installations (D-50): token-authenticated
JSON for programs, not pages, so there is no session and no CSRF token; the bearer token is
the authentication. Errors never include texts, tokens or provider responses."""

from __future__ import annotations

from flask import jsonify, request

from newsreader.services import relay
from newsreader.services.translation import (
    _GLOSSARY_OVERRIDE,
    TARGETS,
    TranslationError,
    active_provider,
    dictionary_available,
    dictionary_lookup,
    translate_titles,
)
from newsreader.web.routing import Routes

routes = Routes()


def _authorised():
    if active_provider() == "relay":
        # an installation that itself translates through a relay does not pass texts on again
        return None, (jsonify({"error": "not a relay"}), 503)
    entry = relay.check_token(request.headers.get("Authorization", ""))
    if entry is None:
        return None, (jsonify({"error": "unauthorized"}), 401)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, (jsonify({"error": "bad request"}), 400)
    return (entry, payload), None


def _client_glossary(value) -> list[tuple[str, str]] | None:
    """A relay client's 訳語の指定: [[name, translation], ...]; None when malformed."""
    from newsreader.services.settings import _glossary_entry

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 200:
        return None
    out = []
    for pair in value:
        entry = _glossary_entry(pair[0], pair[1], "ja") if isinstance(pair, list) and len(pair) == 2 else None
        if entry is None:
            return None
        out.append((entry["source"], entry["target"]))
    return out


@routes.route("/relay/v1/ping", "relay_ping", methods=("POST",))
def relay_ping():
    """For a test build's 「接続を確認」: the token is valid; today's allowance and use. Not charged."""
    found, error = _authorised()
    if error:
        return error
    entry, _payload = found
    used = relay.usage_today().get(entry["hash"][:16], 0)
    return jsonify(
        {"ok": True, "daily_chars": int(entry.get("daily_chars", relay.DEFAULT_DAILY_CHARS)), "used_today": used}
    )


@routes.route("/relay/v1/translate", "relay_translate", methods=("POST",))
def relay_translate():
    found, error = _authorised()
    if error:
        return error
    entry, payload = found
    texts, target = payload.get("texts"), payload.get("target")
    if (
        not isinstance(texts, list)
        or not 0 < len(texts) <= relay.MAX_REQUEST_TEXTS
        or not all(isinstance(t, str) for t in texts)
        or sum(len(t) for t in texts) > relay.MAX_REQUEST_CHARS
        or target not in TARGETS
    ):
        return jsonify({"error": "bad request"}), 400
    glossary = _client_glossary(payload.get("glossary"))
    if glossary is None:
        return jsonify({"error": "bad request"}), 400
    if not relay.charge(entry, sum(len(t) for t in texts)):
        return jsonify({"error": "daily limit"}), 429
    token = _GLOSSARY_OVERRIDE.set(glossary)  # the client's names, never this PC's
    try:
        translated = translate_titles(texts, target, kind="relay", max_chars=relay.MAX_REQUEST_CHARS)
    except TranslationError:
        return jsonify({"error": "translation failed"}), 503
    finally:
        _GLOSSARY_OVERRIDE.reset(token)
    from newsreader.services import sentences

    return jsonify(
        {
            "translations": translated,
            "sentences": [
                sentences.reported(target, text.strip()[: relay.MAX_REQUEST_CHARS], out)
                for text, out in zip(texts, translated)
            ],
        }
    )


@routes.route("/relay/v1/dictionary", "relay_dictionary", methods=("POST",))
def relay_dictionary():
    found, error = _authorised()
    if error:
        return error
    entry, payload = found
    text, target = payload.get("text"), payload.get("target")
    if not isinstance(text, str) or target not in TARGETS:
        return jsonify({"error": "bad request"}), 400
    if not dictionary_available(text, target):
        return jsonify({"senses": []})
    if not relay.charge(entry, len(text)):
        return jsonify({"error": "daily limit"}), 429
    try:
        return jsonify({"senses": dictionary_lookup(text, target)})
    except TranslationError:
        return jsonify({"error": "translation failed"}), 503
