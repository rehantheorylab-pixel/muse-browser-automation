"""Shared fixtures for the browser-automation-v2 unit tests.

Everything external is mocked: no proxy, no network, no live browsers.

- Puts the repo root on sys.path so `core.*` and `rehan.*` import.
- Stubs the third-party `websockets` module (imported at module level by
  core/backends/obscura_backend.py) so `core.router` is importable in a
  bare environment. The stub is never exercised: tests never call the
  network paths of ObscuraBackend.
"""
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Stub third-party `websockets` before any core.* import.
if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.ModuleType("websockets")
