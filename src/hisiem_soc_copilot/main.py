"""Process entrypoint — loads config, builds the app, runs uvicorn.

Only composition lives here: no business logic, SQL, prompts, tool impl, or graph
node logic (python-package-boundary.md §22).
"""

from __future__ import annotations

import uvicorn

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.config import get_settings


def main() -> None:
    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.app.api_host,
        port=settings.app.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
