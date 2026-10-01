"""Uniform error envelope (SPEC §2): {"error": {"code", "message", "details"}}."""

from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from kalpi_engine.domain.schemas import ErrorBody, ErrorEnvelope, Issue


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Sequence[Issue] | dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = list(details) if isinstance(details, Sequence) else details


def envelope(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def _loc(loc: Sequence[object]) -> str:
    return ".".join(str(p) for p in loc if p != "body")


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return envelope(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            Issue(code="INVALID_FIELD", message=f"{_loc(e['loc'])}: {e['msg']}")
            for e in exc.errors()
        ]
        return envelope(422, "VALIDATION_ERROR", "request body is invalid", issues)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        return envelope(exc.status_code, f"HTTP_{exc.status_code}", str(exc.detail))
