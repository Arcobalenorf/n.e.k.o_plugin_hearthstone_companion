from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

_TEXT_LIMIT = 80
_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def canonical_fact_fingerprint(value: Any) -> str:
    """Fingerprint only the canonical fact line returned to an Agent or tool."""

    text = str(value or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    if line.startswith("final_answer="):
        line = line[len("final_answer=") :].strip()
    else:
        match = re.match(r"facts\[[A-Za-z0-9_]+\]=(.*)\Z", line)
        if match is not None:
            line = match.group(1).strip()
    if not line:
        return ""
    return hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest()


def sanitize_diagnostic_code(value: Any, *, default: str = "") -> str:
    text = str(value or default).strip()
    if not text:
        return ""
    bounded = text[:_TEXT_LIMIT]
    return bounded if _CODE_PATTERN.fullmatch(bounded) else "invalid_code"


@dataclass(frozen=True, slots=True)
class RouteDiagnostic:
    status: str = "never"
    reason: str = ""
    observed_at: float = 0.0
    mode: str = ""
    focus: str = ""
    fact_sha256: str = ""
    sequence: int = 0
    submitted_count: int = 0


@dataclass(frozen=True, slots=True)
class ToolRegistrationDiagnostic:
    status: str = "not_checked"
    reason: str = ""
    checked_at: float = 0.0
    missing: tuple[str, ...] = ()
    recovered_count: int = 0
    error_code: str = ""


class DiagnosticTracker:
    """Keep bounded, non-identifying evidence about host delivery paths."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tool_registration = ToolRegistrationDiagnostic()
        self._routes: dict[str, RouteDiagnostic] = {
            "agent": RouteDiagnostic(),
            "lifecycle": RouteDiagnostic(),
            "llm_tool": RouteDiagnostic(),
        }

    def record_tool_registration(
        self,
        result: Mapping[str, Any],
        *,
        observed_at: float | None = None,
    ) -> None:
        checked_at = time.time() if observed_at is None else float(observed_at)
        skipped = bool(result.get("skipped"))
        healthy = bool(result.get("healthy"))
        reason = sanitize_diagnostic_code(result.get("reason"))
        if skipped and reason in {"check_in_flight", "retry_backoff"}:
            return
        if skipped:
            status = "skipped"
        elif healthy:
            status = "healthy"
        else:
            status = "unhealthy"
        missing = tuple(
            dict.fromkeys(
                sanitize_diagnostic_code(item)
                for item in tuple(result.get("missing") or ())[:8]
                if sanitize_diagnostic_code(item)
            )
        )
        recovered = tuple(result.get("recovered") or ())[:8]
        diagnostic = ToolRegistrationDiagnostic(
            status=status,
            reason=reason,
            checked_at=checked_at,
            missing=missing,
            recovered_count=len(recovered),
            error_code=sanitize_diagnostic_code(result.get("error_code")),
        )
        with self._lock:
            self._tool_registration = diagnostic

    def record_route(
        self,
        route: str,
        *,
        status: str,
        reason: str = "",
        mode: str = "",
        focus: str = "",
        fact_sha256: str = "",
        observed_at: float | None = None,
    ) -> None:
        clean_route = sanitize_diagnostic_code(route)
        if clean_route not in self._routes:
            raise ValueError(f"unsupported diagnostic route: {clean_route}")
        with self._lock:
            previous = self._routes[clean_route]
            clean_status = sanitize_diagnostic_code(status, default="unknown")
            diagnostic = RouteDiagnostic(
                status=clean_status,
                reason=sanitize_diagnostic_code(reason),
                observed_at=time.time() if observed_at is None else float(observed_at),
                mode=sanitize_diagnostic_code(mode),
                focus=sanitize_diagnostic_code(focus),
                fact_sha256=(
                    fact_sha256
                    if isinstance(fact_sha256, str)
                    and _SHA256_PATTERN.fullmatch(fact_sha256)
                    else ""
                ),
                sequence=previous.sequence + 1,
                submitted_count=(
                    previous.submitted_count + (1 if clean_status == "submitted" else 0)
                ),
            )
            self._routes[clean_route] = diagnostic

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tool_registration": asdict(self._tool_registration),
                "routes": {
                    name: asdict(value) for name, value in self._routes.items()
                },
            }


__all__ = [
    "DiagnosticTracker",
    "RouteDiagnostic",
    "ToolRegistrationDiagnostic",
    "canonical_fact_fingerprint",
    "sanitize_diagnostic_code",
]
