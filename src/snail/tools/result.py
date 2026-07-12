"""The ToolResult envelope + status taxonomy + speech directives (see docs 03).

Every result the model sees has one consistent shape. Sanitization boundary: the
model gets ``status/reason/retriable/data``; raw errors (stack traces, internals) go
to the log only. Constructors apply the framework directive/reason cascade so callers
get sane defaults and can override per-tool / per-call.
"""

from __future__ import annotations

import enum
from typing import Any

import msgspec


class ToolStatus(enum.Enum):
    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    INVALID_ARGS = "invalid_args"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"
    DEFERRED = "deferred"  # deferred feature (async late-resolve) — docs 07/09§A


class ResponseMode(enum.Enum):
    SPEAK = "speak"
    SILENT = "silent"


class DirectiveMode(enum.Enum):
    HINT = "hint"  # natural-language instruction; model paraphrases (portable default)
    VERBATIM = "verbatim"  # exact words; best-effort only (vendor owns the voice)


class SpeakDirective(msgspec.Struct, frozen=True, kw_only=True):
    text: str
    mode: DirectiveMode = DirectiveMode.HINT


# Framework defaults per status (docs 03, directive cascade).
_DEFAULT_DIRECTIVE: dict[ToolStatus, SpeakDirective] = {
    ToolStatus.ERROR: SpeakDirective(
        text="briefly apologize, say you couldn't process the request"
    ),
    ToolStatus.BLOCKED: SpeakDirective(text="tell the user you're unable to do that"),
    ToolStatus.TIMEOUT: SpeakDirective(
        text="say it's taking too long, ask to try again"
    ),
}
_DEFAULT_REASON: dict[ToolStatus, str] = {
    ToolStatus.ERROR: "the tool failed",
    ToolStatus.BLOCKED: "not permitted",
    ToolStatus.SKIPPED: "handled elsewhere",
    ToolStatus.TIMEOUT: "timed out",
    ToolStatus.INVALID_OUTPUT: "the tool returned an unexpected result",
    ToolStatus.NOT_FOUND: "tool does not exist",
    ToolStatus.CANCELLED: "cancelled",
}


class ToolResult(msgspec.Struct, frozen=True, kw_only=True):
    """The standard contract every ``call_id`` resolves to (exactly once, docs 04)."""

    status: ToolStatus
    data: Any = None  # output_schema-shaped, success only
    reason: str | None = None  # model-facing, sanitized; non-success
    retriable: bool = False
    response_mode: ResponseMode = ResponseMode.SILENT
    speak_directive: SpeakDirective | None = None

    # --- constructors applying the default cascade ---

    @classmethod
    def success(
        cls,
        data: Any = None,
        *,
        response_mode: ResponseMode = ResponseMode.SILENT,
        speak_directive: SpeakDirective | None = None,
    ) -> "ToolResult":
        return cls(
            status=ToolStatus.SUCCESS,
            data=data,
            response_mode=response_mode,
            speak_directive=speak_directive,
        )

    @classmethod
    def error(
        cls,
        reason: str | None = None,
        *,
        retriable: bool = False,
        speak_directive: SpeakDirective | None = None,
    ) -> "ToolResult":
        return cls._nonsuccess(
            ToolStatus.ERROR, reason, retriable=retriable, speak=True,
            speak_directive=speak_directive,
        )

    @classmethod
    def blocked(cls, reason: str | None = None) -> "ToolResult":
        return cls._nonsuccess(ToolStatus.BLOCKED, reason, speak=True)

    @classmethod
    def skipped(cls, reason: str | None = None) -> "ToolResult":
        # handled elsewhere; may never reach the model → silent.
        return cls._nonsuccess(ToolStatus.SKIPPED, reason, speak=False)

    @classmethod
    def invalid_args(cls, detail: str) -> "ToolResult":
        # validation detail so the model self-corrects; retriable, silent.
        return cls(
            status=ToolStatus.INVALID_ARGS,
            reason=detail,
            retriable=True,
            response_mode=ResponseMode.SILENT,
        )

    @classmethod
    def timeout(cls) -> "ToolResult":
        return cls._nonsuccess(ToolStatus.TIMEOUT, None, retriable=True, speak=True)

    @classmethod
    def invalid_output(cls) -> "ToolResult":
        # generic reason to the model; the real detail is a tool-side bug → log only.
        return cls._nonsuccess(ToolStatus.INVALID_OUTPUT, None, speak=False)

    @classmethod
    def not_found(cls, name: str | None = None) -> "ToolResult":
        reason = f"tool {name!r} does not exist" if name else None
        return cls._nonsuccess(ToolStatus.NOT_FOUND, reason, speak=False)

    @classmethod
    def cancelled(cls, reason: str | None = None) -> "ToolResult":
        return cls._nonsuccess(ToolStatus.CANCELLED, reason, speak=False)

    @classmethod
    def _nonsuccess(
        cls,
        status: ToolStatus,
        reason: str | None,
        *,
        retriable: bool = False,
        speak: bool = False,
        speak_directive: SpeakDirective | None = None,
    ) -> "ToolResult":
        return cls(
            status=status,
            reason=reason if reason is not None else _DEFAULT_REASON.get(status),
            retriable=retriable,
            response_mode=ResponseMode.SPEAK if speak else ResponseMode.SILENT,
            speak_directive=(
                speak_directive
                if speak_directive is not None
                else (_DEFAULT_DIRECTIVE.get(status) if speak else None)
            ),
        )
