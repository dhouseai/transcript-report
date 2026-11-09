# Changelog

## 0.1.0 (unreleased)

<!-- When tagging the release, replace "unreleased" with the date, e.g. 2026-11-20. -->

First public release.

- Analyse a video transcript with a local model in LM Studio and save a structured report
  (summary, actionable points, implementation plan, accuracy check, critical analysis,
  glossary, resources, content density and AI-generated scores) as Markdown or PDF.
- Long videos are split into sections, turned into notes and merged to fit the context.
- Transcripts from YouTube's transcript panel (`.txt`) or subtitle files (`.srt`, `.vtt`,
  including YouTube's auto-generated captions).
- `--all` analyses every waiting transcript with the model loaded once.
- `--resume` continues a long video's stopped run from its saved section notes.
- `--check` tests the setup and says what to fix.
- Per-model sampling settings, and a custom prompts folder, in `settings.json`.
- Installable with pip (`python -m pip install git+...`), run as `python -m transcript_report`
  or `transcript-report`. `--init` creates settings.json and the folders in
  `~/transcript-report` (or `$TRANSCRIPT_REPORT_HOME`); running from a clone keeps them in
  the project folder.
- `server_url` / `--server-url` for LM Studio on another computer.
- `api_token` (or `LM_API_TOKEN`) for an LM Studio server that requires an API token.
- Reports are never overwritten: a new folder or file name (`_2`, `_3`, ...) is used instead,
  unless you name the file yourself with `--out`.
- The model is loaded and unloaded every run to free the VRAM, unless it was already loaded.
- PDFs via headless Edge or Chrome; standard library only.
