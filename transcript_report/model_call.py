"""Sending one chat request to LM Studio's /api/v1/chat and streaming the reply (SSE)."""

import json
import re
import time
from collections.abc import Iterable, Iterator
from http.client import HTTPException

from .config import RunOptions
from .errors import TranscriptReportError
from .helpers import finish_progress, progress, strip_think, warn
from .http_client import APIError, JSONClient, error_text


def too_long(opts: RunOptions) -> str:
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
            raise TranscriptReportError(f"{too_long(opts)}\n{e}") from None
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
                                    f"{too_long(opts)}\nThe exact error is in LM Studio's server log "
                                    f"(Developer tab, or ~/.lmstudio/server-logs).")

    stats = result.get("stats", {})
    gen_n = int(stats.get("total_output_tokens", len(out)))
    prompt_n = int(stats.get("input_tokens", 0))
    tps = stats.get("tokens_per_second")
    tps = f"{tps:.1f} tok/s" if isinstance(tps, (int, float)) and tps else "? tok/s"
    progress(f"{label}: done - {gen_n:,} tokens, {tps}, {time.monotonic() - t0:.0f}s", end="\n")
    if prompt_n and prompt_n >= opts.ctx - max_tokens:
        warn("input nearly filled the context window. "
             "Lower --chunk-words or raise --ctx.")
    # LM Studio reports no stop reason, so treat using the whole allowance as hitting the limit.
    cut_off = gen_n >= max_tokens
    if cut_off:
        warn("output hit the token limit and may be incomplete.")

    text = "".join(m.get("content", "") for m in result.get("output", [])
                   if m.get("type") == "message") or "".join(out)
    text = strip_think(text)
    if not text:
        raise TranscriptReportError(f"{label}: the model produced no output outside its reasoning"
                                    + (" (it ran out of tokens while thinking; try without --think)."
                               if cut_off else "."))
    return text
