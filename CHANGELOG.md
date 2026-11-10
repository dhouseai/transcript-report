# Changelog

## 0.2.0 (unreleased)

<!-- When tagging the release, replace "unreleased" with the date, e.g. 2026-11-20. -->

First public release.

- Analyse a video transcript with a local model in **LM Studio or Ollama** and save a
  structured report (summary, actionable points, implementation plan, accuracy check,
  critical analysis, glossary, resources, content density and AI-generated scores) as
  Markdown or PDF.
- Long videos are split into sections, turned into notes and merged to fit the context.
- Transcripts from YouTube's transcript panel (`.txt`) or subtitle files (`.srt`, `.vtt`,
  including YouTube's auto-generated captions).
- `--all` analyses every waiting transcript with the model loaded once.
- `--resume` continues a long video's stopped run from its saved section notes.
- `--check` tests the setup (including the runner, and Ollama's version) and says what to fix.
- Per-model sampling settings, and a custom prompts folder, in `settings.json`.
- Installable with pip (`python -m pip install git+...`), run as `python -m transcript_report`
  or `transcript-report`. `--init` creates settings.json and the folders in
  `~/transcript-report` (or `$TRANSCRIPT_REPORT_HOME`); running from a clone keeps them in
  the project folder.
- **Choosing the runner:** a `"runner"` setting (`"lmstudio"` or `"ollama"`, default
  `"lmstudio"`), a `"runner"` field per model, and `--runner`. `--runner` wins over the
  model's `"runner"`, which wins over the setting. `--port` defaults to the runner's own
  port: 1234 for LM Studio, 11434 for Ollama.
- `server_url` / `--server-url` for the runner on another computer, and `api_token` for a
  server that requires a token (or `LM_API_TOKEN` for LM Studio). `"server_url"` and
  `"api_token"` are for the `"runner"` setting's runner; models on the other runner use it on
  this computer with no token, so one runner's address and token never go to the other's
  server.
- The runner's server is started if it isn't running and stopped afterwards; one that was
  already running is left running.
- The model is loaded and unloaded every run to free the VRAM, unless it was already loaded,
  in which case that copy is used with its own context and left loaded.
- **With Ollama:**
  - The context from settings.json is sent with every request, and a transcript that doesn't
    fit fails as "too long" instead of being cut. If Ollama caps the context at the model's
    maximum, or puts part of the model on the CPU, the script warns.
  - Thinking is off unless `--think` is given; `--think` on a model that can't think gives a
    clear error.
  - A name without a tag, such as `qwen3`, means `qwen3:latest`.
- Reports are never overwritten: a new folder or file name (`_2`, `_3`, ...) is used instead,
  unless you name the file yourself with `--out`.
- PDFs via headless Edge or Chrome; standard library only.
