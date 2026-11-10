import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from transcript_report.config import (
    DEFAULT_CTX,
    PACKAGE_DIR,
    PROMPTS_DIR,
    ModelEntry,
    Settings,
    app_home,
    choose_runner,
    choose_server,
    find_resumable,
    find_transcript,
    init_home,
    list_transcripts,
    load_prompts,
    load_settings,
    move_to_done,
    notes_path_for,
    report_path,
    resolve_model,
    safe_name,
    settings_file,
    unused_path,
)
from transcript_report.errors import TranscriptReportError


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class LoadSettings(TempDirTest):
    def write(self, content) -> Path:
        path = self.dir / "settings.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return path

    def test_missing_file_gives_defaults(self):
        s = load_settings(self.dir / "nope.json")
        self.assertIsNone(s.source)
        self.assertEqual(s.transcripts_dir, self.dir / "transcripts")
        self.assertEqual(s.report_format, "md")

    def test_valid_settings(self):
        s = load_settings(self.write("﻿" + json.dumps({
            "_help": "ignored", "report_format": "PDF", "reports_folder": "out",
            "models": {"m": {"key": " pub/m ", "notes": "x", "sampling": {"temperature": 0.7}}}})))
        self.assertEqual(s.report_format, "pdf")
        self.assertEqual(s.reports_dir, self.dir / "out")
        self.assertEqual(s.models["m"].key, "pub/m")
        self.assertEqual(s.models["m"].ctx, DEFAULT_CTX)
        self.assertEqual(s.models["m"].sampling, {"temperature": 0.7})

    def test_mistakes_are_reported(self):
        bad = ["[1]", "{bad", {"nope": 1}, {"report_format": "doc"}, {"reports_folder": " "},
               {"default_model": 3}, {"models": []}, {"models": {"a": {"ctx": 5}}},
               {"models": {"a": {"key": "k", "ctx": True}}}, {"models": {"a": {"key": "k", "ctx": -1}}},
               {"models": {"a": {"key": "k", "cxt": 5}}},
               {"models": {"a": {"key": "k", "sampling": {"temp": 1}}}},
               {"models": {"a": {"key": "k", "sampling": {"top_p": "high"}}}},
               {"runner": ""}, {"runner": 1}, {"models": {"a": {"key": "k", "runner": ""}}},
               {"models": {"a": {"key": "k", "runner": None}}}]
        for content in bad:
            with self.subTest(content=content), self.assertRaises(TranscriptReportError):
                load_settings(self.write(content))

    def test_runner_setting(self):
        self.assertEqual(load_settings(self.dir / "nope.json").runner, "lmstudio")
        self.assertEqual(load_settings(self.write({"runner": " Ollama "})).runner, "ollama")
        with self.assertRaisesRegex(TranscriptReportError, "'runner' .* must be \"lmstudio\" or \"ollama\""):
            load_settings(self.write({"runner": "vllm"}))

    def test_model_runner(self):
        s = load_settings(self.write({"models": {"a": {"key": "qwen3:14b", "runner": "OLLAMA"},
                                                 "b": {"key": "qwen/qwen3-14b"}}}))
        self.assertEqual((s.models["a"].runner, s.models["b"].runner), ("ollama", ""))
        with self.assertRaisesRegex(TranscriptReportError, "Model 'a' .*\"runner\" must be \"lmstudio\" or"):
            load_settings(self.write({"models": {"a": {"key": "k", "runner": "vllm"}}}))

    def test_missing_key_names_both_runners(self):
        with self.assertRaisesRegex(TranscriptReportError, "`lms ls` for LM Studio, `ollama list` for Ollama"):
            load_settings(self.write({"models": {"a": {"ctx": 5}}}))


class LoadPrompts(TempDirTest):
    def test_builtin_prompts(self):
        p = load_prompts()
        self.assertIn("### Style evidence", p.merge)
        self.assertNotIn("Output only the notes", p.merge)

    def test_custom_folder_falls_back_per_file(self):
        (self.dir / "report_prompt.md").write_text("MY REPORT PROMPT", encoding="utf-8")
        p = load_prompts(self.dir)
        self.assertEqual(p.report, "MY REPORT PROMPT")
        self.assertEqual(p.notes, (PROMPTS_DIR / "section_notes_prompt.md").read_text(encoding="utf-8"))

    def test_notes_prompt_without_marker_is_an_error(self):
        (self.dir / "section_notes_prompt.md").write_text("no format here", encoding="utf-8")
        with self.assertRaises(TranscriptReportError):
            load_prompts(self.dir)


class Paths(TempDirTest):
    def settings(self) -> Settings:
        return Settings(transcripts_dir=self.dir / "in", done_dir=self.dir / "in" / "done",
                        reports_dir=self.dir / "reports")

    def test_safe_name(self):
        self.assertEqual(safe_name('a<b>c:"d'), "a_b_c__d")
        self.assertEqual(len(safe_name("x" * 200)), 80)
        self.assertEqual(safe_name("Title..."), "Title")
        self.assertEqual(safe_name("con"), "_con")
        self.assertEqual(safe_name("..."), "video")

    def test_unused_path_keeps_dots_in_names(self):
        (self.dir / "v1.2 launch_report.md").touch()
        self.assertEqual(unused_path(self.dir / "v1.2 launch_report", ".md"),
                         self.dir / "v1.2 launch_report_2.md")

    def test_report_path_never_reuses_a_folder(self):
        first = report_path(Path("My video.txt"), self.dir, "md")
        self.assertEqual(first, self.dir / "My video" / "My video_report.md")
        first.parent.mkdir()
        self.assertEqual(report_path(Path("My video.txt"), self.dir, "pdf").parent.name, "My video_2")

    def test_find_and_list_transcripts(self):
        s = self.settings()
        s.transcripts_dir.mkdir()
        for name in ("b.txt", "a.vtt", "notes.md"):
            (s.transcripts_dir / name).write_text("x", encoding="utf-8")
        self.assertEqual([p.name for p in list_transcripts(s.transcripts_dir)], ["a.vtt", "b.txt"])
        self.assertEqual(find_transcript(Path("b"), s.transcripts_dir).name, "b.txt")
        self.assertEqual(find_transcript(Path("a"), s.transcripts_dir).name, "a.vtt")
        with self.assertRaises(TranscriptReportError):
            find_transcript(Path("missing.txt"), s.transcripts_dir)

    def test_move_to_done_keeps_both_copies(self):
        s = self.settings()
        s.transcripts_dir.mkdir()
        moved = None
        for _ in range(2):
            t = s.transcripts_dir / "v.txt"
            t.write_text("x", encoding="utf-8")
            moved = move_to_done(t, s)
        assert moved is not None
        self.assertEqual(moved.name, "v_2.txt")
        self.assertTrue((s.done_dir / "v.txt").exists())

    def test_move_to_done_ignores_files_elsewhere(self):
        t = self.dir / "elsewhere.txt"
        t.write_text("x", encoding="utf-8")
        self.assertIsNone(move_to_done(t, self.settings()))
        self.assertTrue(t.exists())

    def test_find_resumable_picks_latest_folder_with_notes(self):
        reports = self.dir / "reports"
        self.assertIsNone(find_resumable(Path("v.txt"), reports, "md"))
        for i, folder in enumerate(("v", "v_2", "v_3", "other")):
            (reports / folder).mkdir(parents=True)
            if folder != "v_3":
                notes = notes_path_for(reports / folder / "v_report.md")
                notes.write_text("x", encoding="utf-8")
                os.utime(notes, (time.time() + i, time.time() + i))
        self.assertEqual(find_resumable(Path("v.txt"), reports, "pdf"), reports / "v_2" / "v_report.pdf")

    def test_find_resumable_skips_finished_reports(self):
        # Notes stay after a run finishes; resuming there would write over the report.
        reports = self.dir / "reports"
        (reports / "v").mkdir(parents=True)
        notes_path_for(reports / "v" / "v_report.md").write_text("x", encoding="utf-8")
        (reports / "v" / "v_report.pdf").write_bytes(b"%PDF")
        self.assertIsNone(find_resumable(Path("v.txt"), reports, "md"))


def with_model(entry: ModelEntry) -> Settings:
    """Settings from a settings.json whose default model "m" is `entry`."""
    return Settings(Path("t"), Path("d"), Path("r"), default_model="m", models={"m": entry},
                    source=Path("s.json"))


class ResolveModel(unittest.TestCase):
    def test_listed_and_unlisted(self):
        s = with_model(ModelEntry("pub/m", 4096, {"top_k": 5}))
        m = resolve_model(None, s)
        self.assertEqual((m.key, m.ctx, m.listed, m.sampling), ("pub/m", 4096, True, {"top_k": 5}))
        m = resolve_model("other/key", s)
        self.assertEqual((m.key, m.ctx, m.listed), ("other/key", DEFAULT_CTX, False))

    def test_no_model_on_first_run_suggests_init(self):
        with self.assertRaisesRegex(TranscriptReportError, "--init"):
            resolve_model(None, Settings(Path("t"), Path("d"), Path("r")))

    def test_example_key_is_rejected(self):
        with self.assertRaisesRegex(TranscriptReportError, "example"):
            resolve_model(None, with_model(ModelEntry("REPLACE-with-a-key-from-lms-ls")))

    def test_listed_model_keeps_its_runner(self):
        s = with_model(ModelEntry("qwen3:14b", runner="ollama"))
        self.assertEqual(resolve_model(None, s).runner, "ollama")
        self.assertEqual(resolve_model("other/key", s).runner, "")   # not listed: no runner of its own


class ChooseRunner(unittest.TestCase):
    """--runner > the model's "runner" > the "runner" setting."""

    def test_precedence(self):
        s = Settings(Path("t"), Path("d"), Path("r"), runner="ollama")
        own = resolve_model("m", Settings(Path("t"), Path("d"), Path("r"),
                                          models={"m": ModelEntry("k", runner="lmstudio")}))
        plain = resolve_model("k", s)
        for runner_arg, model, expected in ((None, None, "ollama"), (None, plain, "ollama"),
                                            (None, own, "lmstudio"), ("ollama", own, "ollama"),
                                            ("lmstudio", plain, "lmstudio")):
            with self.subTest(runner_arg=runner_arg, model=model):
                self.assertEqual(choose_runner(runner_arg, model, s), expected)

    def test_default_is_lm_studio(self):
        self.assertEqual(choose_runner(None, None, Settings(Path("t"), Path("d"), Path("r"))), "lmstudio")


class ChooseServer(unittest.TestCase):
    """The "server_url" and "api_token" settings are for the "runner" setting's runner."""

    def setUp(self):
        self.s = Settings(Path("t"), Path("d"), Path("r"), runner="lmstudio",
                          server_url="http://lm:1234", api_token="lm-token")

    def test_the_settings_runner_gets_the_server_and_token(self):
        self.assertEqual(choose_server(None, self.s, "lmstudio"), ("http://lm:1234", "lm-token"))

    def test_the_other_runner_is_on_this_computer_with_no_token(self):
        self.assertEqual(choose_server(None, self.s, "ollama"), ("", ""))
        local = Settings(Path("t"), Path("d"), Path("r"), runner="lmstudio", api_token="lm-token")
        self.assertEqual(choose_server(None, local, "ollama"), ("", ""))   # LM Studio's token never goes to Ollama

    def test_server_url_arg_changes_only_the_address(self):
        self.assertEqual(choose_server("http://x:1", self.s, "lmstudio"), ("http://x:1", "lm-token"))
        self.assertEqual(choose_server("http://x:11434", self.s, "ollama"), ("http://x:11434", ""))


class Home(TempDirTest):
    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {"TRANSCRIPT_REPORT_HOME": str(self.dir)}):
            self.assertEqual(app_home(), self.dir.resolve())
            self.assertEqual(settings_file(), self.dir.resolve() / "settings.json")

    def test_clone_uses_project_folder(self):
        with mock.patch.dict(os.environ, {"TRANSCRIPT_REPORT_HOME": ""}):
            self.assertEqual(app_home(), PACKAGE_DIR.parent)   # the tests run from the repository

    def test_init_creates_settings_and_folders_once(self):
        path, created, s = init_home(self.dir / "home")
        self.assertTrue(created)
        self.assertTrue(s.transcripts_dir.is_dir() and s.done_dir.is_dir() and s.reports_dir.is_dir())
        self.assertEqual(s.prompts_dir, PROMPTS_DIR)
        path.write_text('{"report_format": "pdf"}', encoding="utf-8")
        _, created, s = init_home(self.dir / "home")
        self.assertFalse(created)
        self.assertEqual(s.report_format, "pdf")   # left as it was


class ServerURL(TempDirTest):
    def test_setting(self):
        path = self.dir / "settings.json"
        path.write_text('{"server_url": " http://192.168.1.20:1234/ "}', encoding="utf-8")
        self.assertEqual(load_settings(path).server_url, "http://192.168.1.20:1234")
        for bad in ("192.168.1.20:1234", "http://", "ftp://x:1", "http://a b"):
            path.write_text(json.dumps({"server_url": bad}), encoding="utf-8")
            with self.subTest(url=bad), self.assertRaises(TranscriptReportError):
                load_settings(path)

    def test_api_token_is_only_the_setting(self):
        # LM Studio's LM_API_TOKEN is read by its runner, so it never goes to Ollama.
        path = self.dir / "settings.json"
        path.write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"LM_API_TOKEN": "from-env"}):
            self.assertEqual(load_settings(path).api_token, "")
            path.write_text('{"api_token": "from-file"}', encoding="utf-8")
            self.assertEqual(load_settings(path).api_token, "from-file")


if __name__ == "__main__":
    unittest.main()
