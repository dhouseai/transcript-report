import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from transcript_report import __version__, cli
from transcript_report.config import Settings
from transcript_report.lmstudio import LoadedInstance


def parse(*argv):
    with contextlib.redirect_stderr(io.StringIO()):
        return cli.parse_args(list(argv))


class ParseArgs(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse("v.txt", "--pdf").format, "pdf")
        self.assertTrue(parse("--all").all)
        self.assertTrue(parse("--check").check)
        self.assertTrue(parse("--init").init)
        self.assertEqual(parse("v.txt", "--server-url", "http://pc:1234/").server_url, "http://pc:1234")

    def test_invalid(self):
        for argv in ([], ["v.txt", "--all"], ["--all", "--title", "x"], ["v.txt", "--chunk-words", "5"],
                     ["v.txt", "--ctx", "0"], ["v.txt", "--out", "r.txt"], ["--all", "--out", "r.md"],
                     ["v.txt", "--pdf", "--md"], ["v.txt", "--temperature", "-1"],
                     ["v.txt", "--server-url", "pc:1234"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                parse(*argv)


class Main(unittest.TestCase):
    def test_streams_that_cant_be_reconfigured(self):
        # e.g. an IDE console: its stand-in streams have no reconfigure()
        with mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.stderr", io.StringIO()), \
             self.assertRaises(SystemExit) as cm:
            cli.main(["--version"], prog="tr")
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(f"tr {__version__}", out.getvalue())


class OutPath(unittest.TestCase):
    def test_choices(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            s = Settings(d / "in", d / "done", d / "reports", report_format="pdf")
            t = Path("My video.txt")
            choose = cli.choose_out_path
            self.assertEqual(choose(None, None, s, t), d / "reports" / "My video" / "My video_report.pdf")
            self.assertEqual(choose(None, "md", s, t).suffix, ".md")
            self.assertEqual(choose(d / "x.md", "pdf", s, t), d / "x.md")
            self.assertEqual(choose(d / "x", None, s, t), d / "x.pdf")
            self.assertEqual(choose(d, "md", s, t), d / "My video_report.md")

    def test_folder_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            s = Settings(d / "in", d / "done", d / "reports")
            (d / "talk_report.md").write_text("earlier", encoding="utf-8")
            self.assertEqual(cli.choose_out_path(d, "md", s, Path("talk.txt")), d / "talk_report_2.md")
            self.assertEqual(cli.choose_out_path(d, "md", s, Path("talk.srt")), d / "talk_report_2.md")


class Batch(unittest.TestCase):
    """run() with LM Studio replaced by a fake model session and chat function."""

    def test_all_skips_failures_and_moves_finished_transcripts(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            s = Settings(d / "in", d / "in" / "done", d / "reports", source=d / "settings.json")
            s.transcripts_dir.mkdir()
            for name, text in (("a.txt", "Fine video."), ("b.txt", "FAIL video."), ("c.txt", "")):
                (s.transcripts_dir / name).write_text(text, encoding="utf-8")

            @contextlib.contextmanager
            def fake_session(*args):
                yield "instance"

            def fake_chat(client, instance, opts, system, user, max_tokens, label):
                if "FAIL" in user:
                    raise cli.TranscriptReportError("too long")
                return "# Report"

            with mock.patch.object(cli, "load_settings", return_value=s), \
                 mock.patch.object(cli, "model_session", fake_session), \
                 mock.patch.object(cli, "chat", fake_chat), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.run(["--all", "--model", "pub/m"])

            self.assertEqual(code, 1)
            self.assertIn("1 of 2 transcripts analysed", err.getvalue())
            self.assertIn("c.txt is empty", err.getvalue())
            self.assertTrue((s.reports_dir / "a" / "a_report.md").is_file())
            self.assertTrue((s.done_dir / "a.txt").is_file())
            self.assertTrue((s.transcripts_dir / "b.txt").is_file())   # failed: left to retry


class SaveReport(unittest.TestCase):
    def test_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            path = cli.save_report("# Hi", Path(d, "sub", "r.md"), "t")
            self.assertEqual(path.read_text(encoding="utf-8"), "# Hi")

    def test_pdf_leaves_existing_markdown_alone(self):
        with tempfile.TemporaryDirectory() as d:
            mine = Path(d, "notes.md")
            mine.write_text("my own notes", encoding="utf-8")

            def fake_pdf(markdown, pdf_path, title):
                pdf_path.write_bytes(b"%PDF")

            with mock.patch.object(cli, "write_pdf", fake_pdf), contextlib.redirect_stderr(io.StringIO()):
                path = cli.save_report("# Hi", Path(d, "notes.pdf"), "t")
            self.assertEqual(path, Path(d, "notes.pdf"))
            self.assertEqual(mine.read_text(encoding="utf-8"), "my own notes")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["notes.md", "notes.pdf"])

    def test_pdf_failure_keeps_markdown_under_a_new_name(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "notes.md").write_text("my own notes", encoding="utf-8")
            with mock.patch.object(cli, "write_pdf", side_effect=RuntimeError("no browser")), \
                 contextlib.redirect_stderr(io.StringIO()):
                path = cli.save_report("# Hi", Path(d, "notes.pdf"), "t")
            self.assertEqual(path, Path(d, "notes_2.md"))
            self.assertEqual(path.read_text(encoding="utf-8"), "# Hi")
            self.assertEqual(Path(d, "notes.md").read_text(encoding="utf-8"), "my own notes")


class ModelSession(unittest.TestCase):
    """model_session() frees what it set up, even when the analysis fails."""

    def run_session(self, loaded=None, fail=False):
        client = mock.Mock(local=False)
        opts = cli.RunOptions(ctx=8192)
        model = cli.ModelChoice("m", "pub/m", 8192, True)
        with mock.patch.object(cli, "ensure_server", return_value=True), \
             mock.patch.object(cli, "loaded_instance", return_value=loaded), \
             mock.patch.object(cli, "load_model", return_value="inst-1"), \
             mock.patch.object(cli, "unload_model") as unload, \
             mock.patch.object(cli, "stop_server") as stop, \
             contextlib.redirect_stderr(io.StringIO()):
            try:
                with cli.model_session(client, model, opts, None) as instance:
                    if fail:
                        raise cli.TranscriptReportError("boom")
            except cli.TranscriptReportError:
                instance = None
        return instance, unload, stop, opts

    def test_loads_then_unloads_and_stops_server_after_failure(self):
        _, unload, stop, _ = self.run_session(fail=True)
        unload.assert_called_once_with(mock.ANY, "inst-1")
        stop.assert_called_once()

    def test_already_loaded_copy_is_used_and_left_loaded(self):
        loaded = LoadedInstance(id="theirs", ctx=16384, parallel=1)
        instance, unload, _, opts = self.run_session(loaded=loaded)
        self.assertEqual(instance, "theirs")
        self.assertEqual(opts.ctx, 16384)
        unload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
