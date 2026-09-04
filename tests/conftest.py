"""Shared pytest configuration.

The project uses a src layout installed in editable mode (``pip install -e .``),
so ``hisiem_soc_copilot`` resolves through normal package imports. ``pytest``'s
declarative ``pythonpath = ["."]`` (pyproject) exposes ``tests.fixtures`` helpers —
no ``sys.path`` manipulation in conftest.

Forces a SelectorEventLoop on Windows so psycopg async works in async tests (the
Windows default ProactorEventLoop is incompatible with psycopg async).
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    import selectors

    class _WindowsSelectorPolicy(asyncio.WindowsSelectorEventLoopPolicy):  # type: ignore[name-defined]
        def new_event_loop(self) -> asyncio.AbstractEventLoop:  # type: ignore[type-arg]
            return asyncio.SelectorEventLoop(selectors.SelectSelector())

    asyncio.set_event_loop_policy(_WindowsSelectorPolicy())
