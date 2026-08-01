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

from .input_required import InputRequired


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
    #: Terminal for the *call*, intermediate for the *run* (docs 14). The run stays in
    #: its agent's slot; the model asks the user and answers via ``provide_input``.
    INPUT_REQUIRED = "input_required"
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
    #: ``input_required`` only: what the blocked run is waiting for (docs 14).
    pending: InputRequired | None = None
    #: ``input_required`` only: the tool that blocked. Kept so the model has context
    #: for the question it is about to ask, and echoes it back on ``provide_input``.
    for_tool: str | None = None

    # --- the wire payload -------------------------------------------------

    def to_payload(self) -> dict:
        """The model-facing object placed in the vendor's function-response.

        The single authority on what a result looks like on the wire. Adapters
        serialize this dict verbatim; they neither add nor drop fields (docs 03's
        sanitization boundary is applied here, once, for every vendor).
        """
        payload: dict = {"status": self.status.value}
        if self.status is ToolStatus.INPUT_REQUIRED:
            p = self.pending
            if p is not None:
                payload["for_tool"] = self.for_tool
                payload["key"] = p.key
                payload["expects"] = p.expects
                payload["ask"] = p.ask
            return payload
        if self.status is ToolStatus.SUCCESS:
            if self.data is not None:
                payload["data"] = self.data
            return payload
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.retriable:
            payload["retriable"] = True
        return payload

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
    def input_required(cls, for_tool: str, pending: InputRequired) -> "ToolResult":
        """Close this *call* while the *run* stays blocked (docs 14).

        Speaks by construction: the whole point is to make the model ask. ``ask`` is
        the directive — a hint, so the model phrases it in its own voice.
        """
        return cls(
            status=ToolStatus.INPUT_REQUIRED,
            for_tool=for_tool,
            pending=pending,
            response_mode=ResponseMode.SPEAK,
            speak_directive=SpeakDirective(text=pending.ask) if pending.ask else None,
        )

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
