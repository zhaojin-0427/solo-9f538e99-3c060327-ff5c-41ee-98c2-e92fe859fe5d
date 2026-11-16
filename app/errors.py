"""Domain errors mapped to HTTP responses in main.py."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class BadPayload(ApiError):
    def __init__(self, message: str, code: str = "bad_payload"):
        super().__init__(422, code, message)


class NotFound(ApiError):
    def __init__(self, message: str):
        super().__init__(404, "not_found", message)


class StaleRevision(ApiError):
    def __init__(self, message: str):
        super().__init__(412, "stale_revision", message)


class LimitExceeded(ApiError):
    def __init__(self, message: str):
        super().__init__(422, "limit_exceeded", message)


class ReportNotFeasible(ApiError):
    def __init__(self, message: str):
        super().__init__(409, "report_not_feasible", message)


class ReportNotCurrent(ApiError):
    def __init__(self, message: str):
        super().__init__(409, "report_not_current", message)


class SourceRevisionChanged(ApiError):
    def __init__(self, message: str):
        super().__init__(409, "source_revision_changed", message)


class InsufficientInferenceWidth(ApiError):
    def __init__(self, concepts: list[str]):
        super().__init__(
            422,
            "insufficient_inference_width",
            "sensitive concepts remain inferable within less than the required "
            f"width; refused: {', '.join(concepts)}",
            {"insufficient_concepts": concepts},
        )
