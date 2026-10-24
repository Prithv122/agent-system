# Vendored from llm-eval-harness (src/evalh/secret_store.py, commit 07354f1),
# copied unchanged below this header.
# Upstream is MIT (same author). Edit here only with a note in NOTES.md.

"""Secret lookup: the environment first, then the OS credential store.

The environment wins, so CI and hosted runners work unchanged. If it has no value, the OS
credential store is asked through the optional `keyring` package (install it with the ``secrets``
extra: ``uv sync --extra secrets``). The service name comes from ``SECRETS_SERVICE`` and falls back
to ``DEFAULT_SERVICE``. Keyring is optional: if it is not installed, or the machine has no
credential backend, a secret simply reads as "not set". Tests set ``SECRETS_NO_KEYRING=1`` so a
key stored on the developer's machine can never turn a replay or "no key" test into a live call.

Nothing here loads a ``.env`` file; export the variable in the shell that runs the command.
"""

from __future__ import annotations

import os

DEFAULT_SERVICE = "deepbrief"


def get_secret(name: str, service: str | None = None) -> str | None:
    """Return the secret called ``name``, or None when it is not set anywhere."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if os.environ.get("SECRETS_NO_KEYRING") == "1":
        return None
    try:
        import keyring

        stored = keyring.get_password(
            service or os.environ.get("SECRETS_SERVICE", DEFAULT_SERVICE), name
        )
    except Exception:  # keyring not installed, or no backend (CI, Linux container)
        return None
    return (stored or "").strip() or None
