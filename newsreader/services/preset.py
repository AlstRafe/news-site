"""A test build's preset (D-53): make_alpha_zip.py ships ``preset/relay_token`` so the build can
translate through the 本体 without a key of its own. On first start the token moves into this
PC's credential store and the file is deleted, so it does not stay on disk next to the app.
(launcher.py applies ``preset/settings.json`` itself, before the app's dependencies exist.)"""
from __future__ import annotations

import logging

from newsreader import paths
from newsreader.services.settings import SecretStoreError, set_secret

log = logging.getLogger(__name__)


def import_relay_token() -> bool:
    """Move a shipped relay token into the credential store. Returns whether one was moved."""
    path = paths.PRESET_TOKEN_PATH
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not token.startswith("nbr_"):
        return False
    try:
        set_secret("relay_token", token)
    except (SecretStoreError, ValueError) as exc:
        log.warning("Could not store the shipped relay token (%s); it stays in preset/ for the next start",
                    type(exc).__name__)
        return False
    path.unlink(missing_ok=True)
    return True
