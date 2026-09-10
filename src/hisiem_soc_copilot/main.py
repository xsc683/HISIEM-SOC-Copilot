"""Process entrypoint — loads config, builds the app, runs uvicorn.

Only composition lives here: no business logic, SQL, prompts, tool impl, or graph
node logic (python-package-boundary.md §22).
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn
from fastapi import FastAPI

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.config import get_settings


def _run_on_selector_loop(app: FastAPI, host: str, port: int) -> None:
    """Serve ``app`` on a SelectorEventLoop (Windows dev runtime).

    psycopg async (the SQLAlchemy async driver) cannot run on the ProactorEventLoop
    that uvicorn defaults to on Windows, so every DB-backed request and the outbox
    dispatcher would fail there. uvicorn 0.52 hard-codes that default and ignores the
    event-loop policy, so the loop factory is passed explicitly here. Non-Windows
    platforms keep uvicorn's own default (Selector/uvloop).
    """
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    asyncio.run(uvicorn.Server(config).serve(), loop_factory=asyncio.SelectorEventLoop)


def main() -> None:
    settings = get_settings()
    app = create_app(settings)
    if sys.platform == "win32":
        _run_on_selector_loop(app, settings.app.api_host, settings.app.api_port)
        return
    uvicorn.run(
        app,
        host=settings.app.api_host,
        port=settings.app.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
