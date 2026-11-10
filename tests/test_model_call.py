import contextlib
import io
import json
import unittest
from typing import Any
from unittest import mock

from transcript_report.config import RunOptions
from transcript_report.errors import TranscriptReportError
from transcript_report.lmstudio import APIError
from transcript_report.model_call import chat, collect, is_context_error, parse_sse

END = b'data: {"type":"chat.end","result":{"stats":{}}}'


def run(lines):
    with contextlib.redirect_stderr(io.StringIO()):
        return collect(parse_sse(lines), "test")


def sse(*events):
    """A streamed reply holding these events, as LM Studio sends them."""
    return io.BytesIO(b"\n".join(b"data: " + json.dumps(e).encode("utf-8") for e in events))


def end(output=None, tokens=10):
    result: dict[str, Any] = {"stats": {"total_output_tokens": tokens, "input_tokens": 100}}
    if output is not None:
        result["output"] = [{"type": "message", "content": output}]
    return {"type": "chat.end", "result": result}


class Stream(unittest.TestCase):
    def test_normal_reply(self):
        out, result = run([b"event: message.delta", b'data: {"type":"message.delta","content":"Hi"}',
                           b"", b'data: {"type":"message.delta","content":" there"}', END])
        self.assertEqual("".join(out), "Hi there")
        self.assertEqual(result, {"stats": {}})

    def test_stream_closed_without_end(self):
        _, result = run([b'data: {"type":"message.delta","content":"Hi"}'])
        self.assertIsNone(result)

    def test_error_event(self):
        with self.assertRaisesRegex(TranscriptReportError, "too big"):
            run([b'data: {"type":"error","error":{"message":"too big"}}'])

    def test_bad_json(self):
        with self.assertRaises(TranscriptReportError):
            run([b"data: {oops"])


class Chat(unittest.TestCase):
    """chat() with LM Studio's reply replaced by a canned stream."""

    def send(self, reply, max_tokens=500, think=False):
        client = mock.Mock()
        client.open.side_effect = [reply]   # returned, or raised if it's an exception
        with contextlib.redirect_stderr(io.StringIO()) as err:
            text = chat(client, "inst", RunOptions(ctx=8192, think=think), "system", "user", max_tokens, "Test")
        return text, err.getvalue()

    def test_final_output_wins_and_reasoning_is_removed(self):
        text, _ = self.send(sse({"type": "message.delta", "content": "partial"},
                                end("<think>hmm</think>The answer")))
        self.assertEqual(text, "The answer")

    def test_streamed_pieces_are_used_without_final_output(self):
        text, _ = self.send(sse({"type": "message.delta", "content": "Hi"},
                                {"type": "message.delta", "content": " there"}, end()))
        self.assertEqual(text, "Hi there")

    def test_using_the_whole_allowance_warns_of_a_cut_off(self):
        _, err = self.send(sse(end("Text", tokens=500)))
        self.assertIn("hit the token limit", err)

    def test_only_reasoning_is_an_error_that_suggests_dropping_think(self):
        with self.assertRaisesRegex(TranscriptReportError, "without --think"):
            self.send(sse(end("<think>still going", tokens=500)), think=True)

    def test_stream_ending_early_points_at_the_context(self):
        with self.assertRaisesRegex(TranscriptReportError, "ended the reply early"):
            self.send(sse({"type": "message.delta", "content": "Hi"}))

    def test_context_error_is_not_an_api_error(self):
        too_long = APIError("LM Studio error 400 on /api/v1/chat: exceeds the context window", 400)
        with self.assertRaises(TranscriptReportError) as cm:
            self.send(too_long)
        self.assertNotIsInstance(cm.exception, APIError)   # so --all skips just this video
        self.assertIn("too long for the 8,192-token context", str(cm.exception))


class ContextErrors(unittest.TestCase):
    def test_recognised(self):
        for msg in ("The number of tokens to keep from the initial prompt is greater than the context length",
                    "Context overflow", "request (40000 tokens) exceeds the context window", "n_ctx too small",
                    "Input is too long"):
            with self.subTest(msg=msg):
                self.assertTrue(is_context_error(msg))

    def test_other_errors_mentioning_context_are_not(self):
        for msg in ("Model not found", "Invalid value for 'context_overflow_policy'",
                    "Error in the request context"):
            with self.subTest(msg=msg):
                self.assertFalse(is_context_error(msg))


if __name__ == "__main__":
    unittest.main()
