"""Unified issue records for validation and wiring reports."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "error"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        row = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        row.update(self.extra)
        return row


def make_issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    **extra,
) -> dict:
    return Issue(code, message, severity=severity, extra=dict(extra)).to_dict()
