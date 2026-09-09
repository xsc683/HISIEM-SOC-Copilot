"""Host-runtime helpers for the operator CLI (Windows event loop).

psycopg async cannot run on the Windows default ProactorEventLoop — it needs a
Selector event loop (tests/conftest.py forces the same policy). The evaluation
CLI runs under :func:`asyncio.run`, so before that loop is created the process
must install a Windows selector policy. This helper lives in the harness package
(the harness is not scanned by the evaluation import-boundary test), and
``evaluation.cli.main`` calls it before dispatching.
"""

from __future__ import annotations

import asyncio
import selectors
import sys


def install_selector_loop_policy() -> None:
    """Install a Windows SelectorEventLoop policy so psycopg async can run.

    No-op on non-Windows platforms (the default policy already yields a selector
    loop there). On Windows, mirrors the policy forced in ``tests/conftest.py``:
    ``asyncio.SelectorEventLoop(selectors.SelectSelector())``.
    """
    if sys.platform != "win32":
        return

    class _WindowsSelectorPolicy(asyncio.WindowsSelectorEventLoopPolicy):
        def new_event_loop(self) -> asyncio.AbstractEventLoop:
            return asyncio.SelectorEventLoop(selectors.SelectSelector())

    asyncio.set_event_loop_policy(_WindowsSelectorPolicy())
