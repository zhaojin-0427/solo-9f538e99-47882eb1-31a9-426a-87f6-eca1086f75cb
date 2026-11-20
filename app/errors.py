"""统一业务错误，返回 4xx 并携带中文说明。"""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ConflictError(ApiError):
    def __init__(self, message: str):
        super().__init__(409, "conflict", message)


class NotFoundError(ApiError):
    def __init__(self, message: str):
        super().__init__(404, "not_found", message)


class BadRequestError(ApiError):
    def __init__(self, message: str):
        super().__init__(400, "bad_request", message)


class UnprocessableError(ApiError):
    """请求语义可解析但业务上无法继续（如缺明细时停止核算）。"""

    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(422, code, message)
        self.extra = extra or {}
