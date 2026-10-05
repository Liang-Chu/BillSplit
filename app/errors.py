from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(what: str = "Resource") -> APIError:
    return APIError(404, "not_found", f"{what} not found")


def forbidden(message: str = "You do not have permission to do this") -> APIError:
    return APIError(403, "forbidden", message)


def validation(message: str, **fields: str) -> APIError:
    return APIError(400, "validation_error", message, {"fields": fields} if fields else {})


def _body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


_STATUS_CODES = {
    400: "validation_error",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "file_too_large",
    415: "unsupported_file_type",
    429: "rate_limited",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError):
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        fields: dict[str, str] = {}
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")]
            fields[".".join(loc) or "_"] = err.get("msg", "invalid")
        return JSONResponse(
            _body("validation_error", "Request validation failed", {"fields": fields}), status_code=400
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = _STATUS_CODES.get(exc.status_code, "error")
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers)
