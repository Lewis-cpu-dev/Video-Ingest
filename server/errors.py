from __future__ import annotations

from typing import Any


class IngestError(Exception):
    def __init__(self, code: str, message: str, stage: str = "validating", retryable: bool = False,
                 next_action: str = "correct_request", retry_after_ms: int | None = None):
        super().__init__(message)
        self.code, self.message, self.stage = code, message, stage
        self.retryable, self.next_action, self.retry_after_ms = retryable, next_action, retry_after_ms

    def as_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "stage": self.stage, "message": self.message,
                  "retryable": self.retryable, "next_action": self.next_action}
        if self.retry_after_ms is not None:
            result["retry_after_ms"] = self.retry_after_ms
        return result
