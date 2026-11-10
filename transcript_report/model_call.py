"""Sending one chat request to LM Studio's /api/v1/chat and streaming the reply (SSE), and
the checks on a finished reply that every runner shares."""

import json
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from http.client import HTTPException

from .config import RunOptions
from .errors import TranscriptReportError
from .helpers import finish_progress, progress, strip_think, warn
from .http_client import APIError, JSONClient, error_text


@dataclass(frozen=True)
class Reply:
    """A finished reply as a runner's stream reader got it, for finish_reply() to check."""
    pieces: list[str]            # the streamed text, in order
    final_text: str              # the whole text if the runner sent it at the end, else ""
    prompt_tokens: int           # 0 if the runner didn't say
    output_tokens: int
    tokens_per_s: float | None   # None if the runner didn't say
    hit_limit: bool | None       # whether it stopped at max_tokens; None if the runner can't tell
    seconds: float               # from sending the request to the end of the reply


def too_long_message(opts: RunOptions) -> str:
    return (f"Input is too long for the {opts.ctx:,}-token context. "
            f"Raise --ctx, or lower --chunk-words or --single-max-words.")


# How llama.cpp and LM Studio word "the input doesn't fit", e.g. "the number of tokens to keep
# from the initial prompt is greater than the context length", "context overflow", "n_ctx".
_CONTEXT_ERROR_RE = re.compile(
    r"context (length|window|size|overflow|shift)|n_ctx|exceeds? the context|"
    r"(prompt|input) (is )?too long|too many tokens", re.IGNORECASE)


def is_context_error(message: str) -> bool:
    """Whether an LM Studio error message says the input doesn't fit the context."""
    return bool(_CONTEXT_ERROR_RE.search(message))


def parse_sse(lines: Iterable[bytes]) -> Iterator[dict]:
    """The JSON events in a server-sent-events stream. "event: ..." lines are skipped,
    because they repeat the data's "type"."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            msg = json.loads(payload)
        except ValueError:
            raise TranscriptReportError(f"LM Studio sent a stream event that isn't JSON: {payload[:200]}") from None
        if isinstance(msg, dict):
            yield msg


def collect(events: Iterable[dict], label: str) -> tuple[list[str], dict | None]:
    """Read the stream: returns (message pieces, the chat.end result or None if the stream
    closed without one). Shows progress while it reads."""
    out: list[str] = []
    thinking = 0
    t0 = last = time.monotonic()
    progress(f"{label}: reading input...")
    for msg in events:
        kind = msg.get("type")
        if kind == "message.delta":
            out.append(msg.get("content", ""))
        elif kind == "reasoning.delta":
            thinking += 1
        elif kind == "error":
            raise TranscriptReportError(f"LM Studio error: {error_text(msg.get('error', msg))}")
        elif kind == "chat.end":
            return out, msg.get("result") or {}
        now = time.monotonic()
        if now - last >= 0.5:
            last = now
            if kind == "prompt_processing.progress":
                state = f"reading input {msg.get('progress', 0):.0%}"
            elif thinking and not out:
                state = f"thinking (~{thinking:,} tokens)"   # deltas, roughly one token each
            else:
                state = f"writing... ~{len(out):,} tokens"
            progress(f"{label}: {state} ({now - t0:.0f}s)")
    return out, None


def chat(client: JSONClient, model_id: str, opts: RunOptions,
         system: str, user: str, max_tokens: int, label: str) -> str:
    """Send one request and return the reply text, with any reasoning removed."""
    return finish_reply(stream_chat(client, model_id, opts, system, user, max_tokens, label),
                        opts, max_tokens, label)


def stream_chat(client: JSONClient, model_id: str, opts: RunOptions,
                system: str, user: str, max_tokens: int, label: str) -> Reply:
    """Send one request to LM Studio and read its streamed reply."""
    body = {
        "model": model_id,
        "system_prompt": system,
        "input": user,
        "stream": True,
        "store": False,                       # don't keep these chats in LM Studio
        "reasoning": "on" if opts.think else "off",
        "max_output_tokens": max_tokens,
        **opts.sampling,
    }
    t0 = time.monotonic()
    try:
        resp = client.open("POST", "/api/v1/chat", body, timeout=3600)
    except APIError as e:
        # A plain TranscriptReportError, not an APIError: in a batch, a video too long for the
        # context is skipped, while losing LM Studio (an APIError with no status) stops the batch.
        if e.status and is_context_error(str(e)):
            raise TranscriptReportError(f"{too_long_message(opts)}\n{e}") from None
        raise
    try:
        with resp:
            out, result = collect(parse_sse(resp), label)
    except (OSError, HTTPException) as e:   # reset, timeout, LM Studio closed mid-reply
        finish_progress()
        raise APIError(f"{label}: lost the connection to LM Studio ({e}).") from None

    if result is None:
        # LM Studio closes the stream without an error event when it rejects a request,
        # most often because the input doesn't fit the context.
        finish_progress()
        raise TranscriptReportError(f"{label}: LM Studio ended the reply early. The most likely cause: "
                                    f"{too_long_message(opts)}\nThe exact error is in LM Studio's server log "
                                    f"(Developer tab, or ~/.lmstudio/server-logs).")

    stats = result.get("stats", {})
    tps = stats.get("tokens_per_second")
    return Reply(
        pieces=out,
        final_text="".join(m.get("content", "") for m in result.get("output", [])
                           if m.get("type") == "message"),
        prompt_tokens=int(stats.get("input_tokens", 0)),
        output_tokens=int(stats.get("total_output_tokens", len(out))),
        tokens_per_s=tps if isinstance(tps, (int, float)) and tps else None,
        hit_limit=None,   # LM Studio reports no stop reason
        seconds=time.monotonic() - t0)


def finish_reply(reply: Reply, opts: RunOptions, max_tokens: int, label: str) -> str:
    """Report on a finished reply, warn if it nearly filled the context or was cut off, and
    return its text with any reasoning removed. The same checks for every runner."""
    tps = f"{reply.tokens_per_s:.1f} tok/s" if reply.tokens_per_s else "? tok/s"
    progress(f"{label}: done - {reply.output_tokens:,} tokens, {tps}, {reply.seconds:.0f}s", end="\n")
    if reply.prompt_tokens and reply.prompt_tokens >= opts.ctx - max_tokens:
        warn("input nearly filled the context window. "
             "Lower --chunk-words or raise --ctx.")
    # Without a stop reason, using the whole allowance counts as hitting the limit.
    cut_off = reply.output_tokens >= max_tokens if reply.hit_limit is None else reply.hit_limit
    if cut_off:
        warn("output hit the token limit and may be incomplete.")

    text = strip_think(reply.final_text or "".join(reply.pieces))
    if not text:
        why = ""
        if cut_off:
            # Some models reason even with reasoning off, so suggest dropping --think only if it was given.
            why = " (it ran out of tokens while thinking" + ("; try without --think)" if opts.think else ")")
        raise TranscriptReportError(f"{label}: the model produced no output outside its reasoning{why}.")
    return text
