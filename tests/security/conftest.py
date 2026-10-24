"""Security regression tests. Reuses the in-process API + worker harness from the integration suite."""

from __future__ import annotations

from tests.integration.conftest import Harness, harness, make_harness  # noqa: F401 — re-exported fixtures
from tests.integration.test_auth_flows import entra  # noqa: F401 — re-exported fixture

__all__ = ["Harness"]
