import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from transcript_report import __version__, cli
from transcript_report.config import ModelEntry, Settings
from transcript_report.runner import LoadedModel


class FakeRunner:
    """Stands in for a Runner on another computer: records the calls that set things up
    or free them. chat() fails for a transcript containing "FAIL"."""

    name = "ExampleLM"
    list_hint = "run `fake ls`"
    cli = "fake command"
    on_this_computer = False
    url = "http://fake:1"

    def __init__(self, loaded: LoadedModel | None = None, started: bool = True, keys=("pub/m",)):
        self.loaded_copy, self.started, self.keys = loaded, started, list(keys)
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")
        return self.started

    def stop(self):
        self.calls.append("stop")

    def find_cli(self):
        return "fake"

    def model_keys(self):
        return self.keys

    def has_model(self, key):
        return key in self.keys

    def loaded(self, key):
        return self.loaded_copy

    def load(self, key, ctx):
        self.calls.append(f"load {key} {ctx}")
        return LoadedModel(id="inst-1", ctx=ctx, parallel=1)

    def unload(self, model_id):
        self.calls.append(f"unload {model_id}")
        return True

    def chat(self, model_id, opts, system, user, max_tokens, label):
        if "FAIL" in user:
            raise cli.TranscriptReportError("too long")
        return "# Report"


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
        self.assertEqual(parse("v.txt", "--runner", "ollama").runner, "ollama")

    def test_runner_and_port_are_left_to_the_settings_and_the_runner(self):
        args = parse("v.txt")
        self.assertIsNone(args.runner)
        self.assertIsNone(args.port)   # each runner has its own default port

    def test_invalid(self):
        for argv in ([], ["v.txt", "--all"], ["--all", "--title", "x"], ["v.txt", "--chunk-words", "5"],
                     ["v.txt", "--ctx", "0"], ["v.txt", "--out", "r.txt"], ["--all", "--out", "r.md"],
                     ["v.txt", "--pdf", "--md"], ["v.txt", "--temperature", "-1"],
                     ["v.txt", "--server-url", "pc:1234"], ["v.txt", "--runner", "vllm"]):
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
    """run() with LM Studio replaced by a fake runner."""

    def test_all_skips_failures_and_moves_finished_transcripts(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            s = Settings(d / "in", d / "in" / "done", d / "reports", source=d / "settings.json")
            s.transcripts_dir.mkdir()
            for name, text in (("a.txt", "Fine video."), ("b.txt", "FAIL video."), ("c.txt", "")):
                (s.transcripts_dir / name).write_text(text, encoding="utf-8")
            runner = FakeRunner()

            with mock.patch.object(cli, "load_settings", return_value=s), \
                 mock.patch.object(cli, "make_runner", return_value=runner), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.run(["--all", "--model", "pub/m"])

            self.assertEqual(runner.calls[-2:], ["unload inst-1", "stop"])   # once, after the batch
            self.assertEqual(code, 1)
            self.assertIn("1 of 2 transcripts analysed", err.getvalue())
            self.assertIn("c.txt is empty", err.getvalue())
            self.assertTrue((s.reports_dir / "a" / "a_report.md").is_file())
            self.assertTrue((s.done_dir / "a.txt").is_file())
            self.assertTrue((s.transcripts_dir / "b.txt").is_file())   # failed: left to retry


class RunnerChoice(unittest.TestCase):
    """run(), --check and --init build the runner picked by --runner, the model or the setting."""

    def runner_built(self, *argv, runner="lmstudio"):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            s = Settings(d / "in", d / "done", d / "reports", source=d / "settings.json", runner=runner,
                         models={"ol": ModelEntry("qwen3:14b", runner="ollama"), "lm": ModelEntry("pub/m")})
            s.transcripts_dir.mkdir()
            (s.transcripts_dir / "v.txt").write_text("A video.", encoding="utf-8")
            with mock.patch.object(cli, "load_settings", return_value=s), \
                 mock.patch.object(cli, "make_runner", return_value=FakeRunner()) as make, \
                 contextlib.redirect_stderr(io.StringIO()):
                cli.run(["v.txt", *argv])
        return make.call_args.args

    def test_precedence(self):
        self.assertEqual(self.runner_built("--model", "lm")[0], "lmstudio")
        self.assertEqual(self.runner_built("--model", "lm", runner="ollama")[0], "ollama")
        self.assertEqual(self.runner_built("--model", "ol")[0], "ollama")
        self.assertEqual(self.runner_built("--model", "ol", "--runner", "lmstudio")[0], "lmstudio")

    def test_port_is_left_to_the_runner_unless_given(self):
        self.assertIsNone(self.runner_built("--model", "ol")[1])
        self.assertEqual(self.runner_built("--model", "ol", "--port", "5000")[1], 5000)

    def test_check_gets_the_runner_and_port(self):
        with mock.patch.object(cli, "run_check", return_value=0) as check:
            cli.run(["--check", "--runner", "ollama"])
        check.assert_called_once_with(None, "ollama", None, None)

    def test_init_names_the_runners_list_command(self):
        s = Settings(Path("t"), Path("d"), Path("r"))
        for argv, hint in (([], "`lms ls`"), (["--runner", "ollama"], "`ollama list`")):
            with self.subTest(argv=argv), \
                 mock.patch.object(cli, "init_home", return_value=(Path("settings.json"), True, s)), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                cli.run(["--init", *argv])
            self.assertIn(f"\"key\" (run {hint} to see them)", out.getvalue())


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

    def run_session(self, runner, fail=False, listed=True):
        opts = cli.RunOptions(ctx=8192)
        model = cli.ModelChoice("m", "pub/m", 8192, listed)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with cli.model_session(runner, model, opts, None) as model_id:
                if fail:
                    raise cli.TranscriptReportError("boom")
        return model_id, opts, err.getvalue()

    def test_loads_then_unloads_and_stops_server_after_failure(self):
        runner = FakeRunner()
        with self.assertRaisesRegex(cli.TranscriptReportError, "boom"):
            self.run_session(runner, fail=True)
        self.assertEqual(runner.calls, ["start", "load pub/m 8192", "unload inst-1", "stop"])

    def test_already_loaded_copy_is_used_and_left_loaded(self):
        runner = FakeRunner(loaded=LoadedModel(id="theirs", ctx=16384, parallel=1))
        model_id, opts, err = self.run_session(runner)
        self.assertEqual(model_id, "theirs")
        self.assertEqual(opts.ctx, 16384)
        self.assertIn("already loaded in ExampleLM", err)
        self.assertEqual(runner.calls, ["start", "stop"])

    def test_server_is_stopped_only_if_this_run_started_it(self):
        runner = FakeRunner(started=False)
        self.run_session(runner)
        self.assertEqual(runner.calls, ["start", "load pub/m 8192", "unload inst-1"])

    def test_model_key_the_server_doesnt_have(self):
        runner = FakeRunner(keys=["other/model"])
        with self.assertRaisesRegex(cli.TranscriptReportError, "an ExampleLM model key .*other/model"):
            self.run_session(runner, listed=False)
        self.assertEqual(runner.calls, ["start", "stop"])


if __name__ == "__main__":
    unittest.main()
