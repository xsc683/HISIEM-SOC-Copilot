"""FastAPI exception → JSON error mapping.

Only stable codes and analyst-safe messages cross the HTTP boundary; upstream
detail never leaks (per the HISIEM agent-integration normalization pattern).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..application.errors import (
    ApplicationError,
    ExternalServiceError,
    NotFoundError,
    UnauthorizedError,
    to_http_error,
)
from ..domain.shared.errors import DomainError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        status, code, message = to_http_error(exc)
        return JSONResponse(status_code=status, content={"code": code, "message": message})

    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        status, code, message = to_http_error(exc)
        return JSONResponse(status_code=status, content={"code": code, "message": message})

    @app.exception_handler(ExternalServiceError)
    async def _external(_: Request, exc: ExternalServiceError) -> JSONResponse:
        status, code, message = to_http_error(exc)
        return JSONResponse(status_code=status, content={"code": code, "message": message})

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized(_: Request, exc: UnauthorizedError) -> JSONResponse:
        status, code, message = to_http_error(exc)
        return JSONResponse(status_code=status, content={"code": code, "message": message})

    @app.exception_handler(ApplicationError)
    async def _application(_: Request, exc: ApplicationError) -> JSONResponse:
        status, code, message = to_http_error(exc)
        return JSONResponse(status_code=status, content={"code": code, "message": message})
