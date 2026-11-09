import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from transcript_report.config import RunOptions, load_prompts
from transcript_report.errors import TranscriptReportError
from transcript_report.pipeline import analyse, condense, split_sections

PROMPTS = load_prompts()
LONG_TEXT = " ".join(f"word{i}." for i in range(40000))


class FakeChat:
    """Stands in for model_call.chat: records the labels, returns `reply`."""

    def __init__(self, reply: str = "note " * 300, fail_on: int | None = None):
        self.reply, self.fail_on, self.labels = reply, fail_on, []

    def __call__(self, system, user, max_tokens, label):
        self.labels.append(label)
        if len(self.labels) == self.fail_on:
            raise TranscriptReportError("boom")
        return self.reply


def quiet():
    return contextlib.redirect_stderr(io.StringIO())


class SplitSections(unittest.TestCase):
    def test_limit_order_and_overlap(self):
        text = " ".join(f"w{i}." for i in range(1050))
        sections = split_sections(text, 500)
        self.assertTrue(all(len(s.split()) <= 500 for s in sections))
        self.assertEqual(sections[0].split()[0], "w0.")
        self.assertEqual(sections[-1].split()[-1], "w1049.")
        # the next section starts with a few words from the end of the previous one
        first_of_next = sections[1].split()[0]
        self.assertIn(first_of_next, sections[0].split()[-120:])

    def test_sizes_are_evened_out(self):
        sizes = [len(s.split()) for s in split_sections(" ".join(f"w{i}." for i in range(1050)), 500)]
        self.assertGreater(min(sizes), 200)

    def test_unpunctuated_text_is_cut(self):
        sections = split_sections(" ".join(["word"] * 5000), 1000)
        self.assertTrue(all(len(s.split()) <= 1000 for s in sections))
        self.assertGreaterEqual(len(sections), 5)

    def test_short_text_is_one_section(self):
        self.assertEqual(split_sections("Just this.", 500), ["Just this."])


class Analyse(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.notes = Path(self._tmp.name, "r", "v_report_section_notes.md")

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_pass(self):
        chat = FakeChat("REPORT")
        with quiet():
            self.assertEqual(analyse(RunOptions(ctx=32768), PROMPTS, "Short video.", self.notes, chat), "REPORT")
        self.assertEqual(chat.labels, ["Writing report"])
        self.assertFalse(self.notes.exists())

    def test_long_video_saves_notes_and_writes_report(self):
        chat = FakeChat()
        with quiet():
            analyse(RunOptions(ctx=16384), PROMPTS, LONG_TEXT, self.notes, chat)
        self.assertEqual(chat.labels[-1], "Writing final report")
        sections = len(chat.labels) - 1
        self.assertEqual(self.notes.read_text(encoding="utf-8").count("## Notes for section"), sections)

    def stop_at_third_call(self):
        with quiet(), self.assertRaises(TranscriptReportError):
            analyse(RunOptions(ctx=16384), PROMPTS, LONG_TEXT, self.notes, FakeChat(fail_on=3))

    def test_failed_run_keeps_notes_and_resume_continues(self):
        self.stop_at_third_call()
        self.assertEqual(self.notes.read_text(encoding="utf-8").count("## Notes for section"), 2)

        chat = FakeChat()
        with quiet():
            analyse(RunOptions(ctx=16384), PROMPTS, LONG_TEXT, self.notes, chat, resume=True)
        self.assertTrue(chat.labels[0].startswith("Section 3/"))

    def test_resume_ignores_notes_from_a_different_split(self):
        self.stop_at_third_call()
        chat = FakeChat()
        with quiet():
            analyse(RunOptions(ctx=16384, chunk_words=3000), PROMPTS, LONG_TEXT, self.notes, chat,
                    resume=True)
        self.assertTrue(chat.labels[0].startswith("Section 1/"))

    def test_think_needs_more_context(self):
        with quiet(), self.assertRaises(TranscriptReportError):
            analyse(RunOptions(ctx=8192, think=True), PROMPTS, LONG_TEXT, self.notes, FakeChat())

    def test_condense_merges_until_it_fits(self):
        chat = FakeChat("merged " * 400)
        notes = ["## n\n" + "x " * 1000 for _ in range(12)]
        with quiet():
            out = condense(RunOptions(ctx=16384), PROMPTS, notes, 50000, self.notes, chat)
        self.assertLess(len(out), len(notes))
        self.assertTrue(all(label.startswith("Merging batch") for label in chat.labels))


if __name__ == "__main__":
    unittest.main()
