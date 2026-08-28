"""Fold .env into the environment, once, before anything reads it.

Without this a key sits in .env doing nothing: every port reads os.environ
directly, so the file is inert and the failure looks like a bad credential
rather than a missing loader. That is an expensive hour.

Existing environment variables always win, so a deployment's real settings are
never overwritten by a stray file left in the image.
"""

from __future__ import annotations

import os
import pathlib

_loaded = False


def load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = pathlib.Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")
