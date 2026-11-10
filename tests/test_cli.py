import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from transcript_report import __version__, cli
from transcript_report.config import ModelEntry, Settings
from transcript_report.ollama import full_name, same_model
from transcript_report.runner import LoadedModel


class FakeRunner:
    """Stands in for a Runner, set up like LM Studio: an instance id, one request slot, the
    context asked for, exact model keys. Records the calls that set things up or free them.
    chat() fails for a transcript containing "FAIL"."""

    name = "ExampleLM"
    list_hint = "run `fake ls`"
    cli = "fake command"
    url = "http://fake:1"

    def __init__(self, loaded: LoadedModel | None = None, started: bool = True, keys=("pub/m",),
                 on_this_computer: bool = False):
        self.loaded_copy, self.started, self.keys = loaded, started, list(keys)
        self.on_this_computer = on_this_computer
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


class FakeOllama(FakeRunner):
    """A fake Runner set up like Ollama: the model's full name as its id, no parallel count,
    the context capped at the model's maximum, and a bare name matching its :latest tag."""

    name = "ExampleOllama"

    def __init__(self, loaded: LoadedModel | None = None, started: bool = True, keys=("qwen3:latest",),
                 on_this_computer: bool = False, max_ctx: int = 0):
        super().__init__(loaded, started, keys, on_this_computer)
        self.max_ctx = max_ctx

    def has_model(self, key):
        return any(same_model(k, key) for k in self.keys)

    def load(self, key, ctx):
        self.calls.append(f"load {key} {ctx}")
        return LoadedModel(id=full_name(key), ctx=min(ctx, self.max_ctx or ctx), parallel=0)


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
                         server_url="http://lm:1234", api_token="lm-token",
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

    def test_server_and_token_follow_the_runner(self):
        self.assertEqual(self.runner_built("--model", "lm")[2:], ("http://lm:1234", "lm-token"))
        # The server and token settings are LM Studio's here, so an Ollama model gets neither.
        self.assertEqual(self.runner_built("--model", "ol")[2:], ("", ""))
        self.assertEqual(self.runner_built("--model", "lm", "--runner", "ollama")[2:], ("", ""))
        self.assertEqual(self.runner_built("--model", "ol", runner="ollama")[2:], ("http://lm:1234", "lm-token"))
        # --server-url changes only the address.
        self.assertEqual(self.runner_built("--model", "lm", "--server-url", "http://pc:1234")[2:],
                         ("http://pc:1234", "lm-token"))
        self.assertEqual(self.runner_built("--model", "ol", "--server-url", "http://pc:11434")[2:],
                         ("http://pc:11434", ""))

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


class SessionTests:
    """model_session() frees what it set up, even when the analysis fails. Mixed into one
    TestCase per kind of runner, each with a fake set up like that runner."""

    Runner: type[FakeRunner]
    KEY: str          # a model key the fake has
    LOADED_ID: str    # the id the fake's load() hands out for KEY
    PARALLEL: int     # what the runner reports for a loaded copy's request slots

    def run_session(self, runner, fail=False, listed=True, ctx_arg=None, key=None):
        opts = cli.RunOptions(ctx=8192)
        model = cli.ModelChoice("m", key or self.KEY, 8192, listed)
        with contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.object(cli, "check_vram") as vram:
            with cli.model_session(runner, model, opts, ctx_arg) as model_id:
                if fail:
                    raise cli.TranscriptReportError("boom")
        self.vram = vram
        return model_id, opts, err.getvalue()

    def test_loads_then_unloads_and_stops_server_after_failure(self):
        runner = self.Runner()
        with self.assertRaisesRegex(cli.TranscriptReportError, "boom"):
            self.run_session(runner, fail=True)
        self.assertEqual(runner.calls, ["start", f"load {self.KEY} 8192", f"unload {self.LOADED_ID}", "stop"])

    def test_yields_the_id_load_hands_out(self):
        model_id, _, err = self.run_session(self.Runner())
        self.assertEqual(model_id, self.LOADED_ID)
        self.assertNotIn("WARNING", err)

    def test_already_loaded_copy_is_used_and_left_loaded(self):
        runner = self.Runner(loaded=LoadedModel(id="theirs", ctx=16384, parallel=self.PARALLEL))
        model_id, opts, err = self.run_session(runner)
        self.assertEqual(model_id, "theirs")
        self.assertEqual(opts.ctx, 16384)
        self.assertIn(f"already loaded in {runner.name}", err)
        self.assertNotIn("WARNING", err)
        self.assertEqual(runner.calls, ["start", "stop"])

    def test_ctx_arg_is_ignored_for_a_loaded_copy_of_another_size(self):
        runner = self.Runner(loaded=LoadedModel(id="theirs", ctx=16384, parallel=self.PARALLEL))
        _, opts, err = self.run_session(runner, ctx_arg=4096)
        self.assertEqual(opts.ctx, 16384)
        self.assertIn(f"--ctx 4,096 is ignored: the loaded copy has a 16,384-token context. Unload it in "
                      f"{runner.name}", err)

    def test_loaded_copy_without_a_context_keeps_the_one_asked_for(self):
        runner = self.Runner(loaded=LoadedModel(id="theirs", ctx=0, parallel=0))
        _, opts, err = self.run_session(runner)
        self.assertEqual(opts.ctx, 8192)
        self.assertIn(f"{runner.name} didn't report the loaded copy's context size; assuming 8,192", err)

    def test_server_is_stopped_only_if_this_run_started_it(self):
        runner = self.Runner(started=False)
        self.run_session(runner)
        self.assertEqual(runner.calls, ["start", f"load {self.KEY} 8192", f"unload {self.LOADED_ID}"])

    def test_model_key_the_server_doesnt_have(self):
        runner = self.Runner(keys=["other/model:7b"])
        with self.assertRaisesRegex(cli.TranscriptReportError, f"an {runner.name} model key .*other/model:7b"):
            self.run_session(runner, listed=False)
        self.assertEqual(runner.calls, ["start", "stop"])

    def test_vram_is_checked_only_on_this_computer(self):
        self.run_session(self.Runner(on_this_computer=True))
        self.vram.assert_called_once_with("lower --ctx (now 8,192)")
        self.run_session(self.Runner())
        self.vram.assert_not_called()

    def test_vram_advice_for_a_loaded_copy(self):
        runner = self.Runner(loaded=LoadedModel(id="theirs", ctx=16384, parallel=self.PARALLEL),
                             on_this_computer=True)
        self.run_session(runner)
        self.vram.assert_called_once_with(f"unload other models in {runner.name}")


class LMStudioSession(SessionTests, unittest.TestCase):
    Runner = FakeRunner
    KEY = "pub/m"
    LOADED_ID = "inst-1"
    PARALLEL = 1

    def test_loaded_copy_with_parallel_requests_warns(self):
        _, _, err = self.run_session(self.Runner(loaded=LoadedModel(id="theirs", ctx=16384, parallel=4)))
        self.assertIn("allows 4 parallel requests", err)


class OllamaSession(SessionTests, unittest.TestCase):
    Runner = FakeOllama
    KEY = "qwen3"
    LOADED_ID = "qwen3:latest"
    PARALLEL = 0   # Ollama doesn't report one, so the parallel warning never shows

    def test_bare_name_matches_its_latest_tag(self):
        runner = FakeOllama(keys=["qwen3:latest"])
        model_id, _, _ = self.run_session(runner, listed=False)
        self.assertEqual(model_id, "qwen3:latest")
        self.assertEqual(runner.calls, ["start", "load qwen3 8192", "unload qwen3:latest", "stop"])

    def test_capped_context_is_used(self):
        runner = FakeOllama(max_ctx=4096, on_this_computer=True)
        _, opts, _ = self.run_session(runner)
        self.assertEqual(opts.ctx, 4096)
        self.vram.assert_called_once_with("lower --ctx (now 4,096)")


if __name__ == "__main__":
    unittest.main()
