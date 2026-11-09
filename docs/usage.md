# Usage

```
python -m transcript_report "<transcript name>.txt" --title "Video title - Channel"
python -m transcript_report --all
python -m transcript_report --init
python -m transcript_report --check
```

`transcript-report ...` (the installed command) and `python analyse_video.py ...` (from a
clone) take exactly the same options. On Windows with Smart App Control on, the
`transcript-report` command is blocked because pip's launcher isn't signed; use
`python -m transcript_report`.

## Where things are kept

settings.json, and the default transcripts and reports folders, live in the **home folder**:

- the folder in the `TRANSCRIPT_REPORT_HOME` environment variable, if it's set;
- otherwise, when running from a clone of the repository, the project folder;
- otherwise (installed with pip), `transcript-report` in your home folder, e.g.
  `C:\Users\you\transcript-report` or `~/transcript-report`.

`--init` creates settings.json (from the example) and the folders there, and never
overwrites an existing settings.json. `--check` and `--help` show which home folder is used.
Relative folder paths in settings.json are relative to the folder settings.json is in.

## Getting a transcript

**From YouTube's transcript panel (`.txt`).** Under the video, expand the description and
click **Show transcript**. Select the whole transcript in the panel, copy it, and paste it
into a `.txt` file in the `transcripts` folder. The timestamps are removed
automatically, so paste it exactly as it comes.

**As a subtitle file (`.srt` / `.vtt`).** Any `.srt` or `.vtt` file works, including
YouTube's auto-generated captions downloaded with [yt-dlp](https://github.com/yt-dlp/yt-dlp):

```
yt-dlp --skip-download --write-auto-subs --sub-langs en --sub-format vtt -o "%(title)s" <video URL>
```

Timings, styling, `[Music]`-style tags and the repeated lines of YouTube's rolling captions
are all removed. Auto-generated captions have no punctuation, so the model has a slightly
harder job than with a manual transcript.

The file name becomes the report name, so name it after the video.

## What a run does

- Starts LM Studio's server if it isn't running, and stops it again afterwards.
- Loads the model, and unloads it at the end so the VRAM is free again. If the model is
  already loaded in LM Studio, that copy is used with the context it was loaded with, and
  it stays loaded.
- Short transcripts are analysed in one pass. Long ones are split into sections. Each
  section's notes are saved to `<video name>_report_section_notes.md` as soon as they're
  done, and the notes are then combined into the report.
- The report goes to `<reports folder>/<video name>/<video name>_report.md` (or `.pdf`).
  If that folder already exists, `<video name>_2`, `_3`, ... is used, so nothing is
  overwritten. Very long names are shortened to 80 characters.
- Once the report is saved, the transcript is **moved** from the transcripts folder into the
  done folder. Transcripts given by a path somewhere else are left where they are.
- Ctrl+C stops the run at any point and still unloads the model.

## Options

| Option | What it does |
|---|---|
| `--title "..."` | Video title / channel, gives the model context |
| `--all` | Analyse every transcript waiting in the transcripts folder, loading the model once. A video that fails is reported and skipped |
| `--resume` | Continue a long video's stopped run: sections whose notes were already saved are skipped. Use the same `--ctx` and `--chunk-words` as before |
| `--init` | Create settings.json and the transcripts and reports folders in the home folder |
| `--check` | Test the setup (settings, prompts, LM Studio, the model, a PDF browser, the GPU) and say what to fix |
| `--model <name>` | A name from `"models"` in settings.json, or an LM Studio key from `lms ls` (context then defaults to 32768 unless `--ctx`) |
| `--ctx <tokens>` | Context size (default: the model's `"ctx"` in settings.json) |
| `--pdf` / `--md` | Save as PDF or Markdown (default: `"report_format"`) |
| `--out <file or folder>` | Where to save the report. Ending it in `.pdf` or `.md` also picks the format (a file given this way is overwritten). In a folder, an existing report is never overwritten: `_2`, `_3`, ... is added. With `--all` it must be a folder |
| `--think` | Let the model reason before answering. Slower, and needs more context, because reasoning counts against the output limit |
| `--chunk-words <n>` | Words per section for long videos (default 10000; lowered automatically if a section wouldn't fit the context) |
| `--single-max-words <n>` | Longer transcripts are split into sections (default 15000, about 100 minutes of speech) |
| `--temperature <t>` | Sampling temperature (default: the model's `"sampling"`, else 0.4) |
| `--port <n>` | Port for LM Studio's server on this computer, if it isn't running (default 1234) |
| `--lmstudio-url <url>` | LM Studio on another computer, e.g. `http://192.168.1.20:1234` (default: `"lmstudio_url"`) |
| `--version` | Print the version |

## settings.json

`--init` creates it in the home folder (see [Where things are kept](#where-things-are-kept)).
Every setting is optional; leave one out and its default is used. Settings starting with `_`
are just notes.

| Setting | What it is | Default |
|---|---|---|
| `transcripts_folder` | Where transcripts are read from | `transcripts` |
| `done_folder` | Where a transcript is moved once its report is saved | `transcripts/done` |
| `reports_folder` | Where reports go, one folder per video | `reports` |
| `prompts_folder` | A folder with your own prompt files (see below) | empty: the built-in prompts |
| `report_format` | `"md"` or `"pdf"`: what reports are saved as | `md` |
| `lmstudio_url` | LM Studio on another computer (see below) | empty: this computer |
| `lmstudio_api_token` | LM Studio API token, if its server requires one (see below) | empty: the `LM_API_TOKEN` environment variable, else none |
| `default_model` | The model used when `--model` isn't given | |
| `models` | The models you can pick, by name (below) | |

```json
"models": {
  "qwen3.5-9b": {
    "key": "transcript-report/qwen3.5-9b",
    "ctx": 65536,
    "sampling": {"temperature": 0.4, "top_p": 0.8, "top_k": 20},
    "notes": "anything you like"
  }
}
```

- `key`: the LM Studio model key, from `lms ls`. Required.
- `ctx`: the context size in tokens (default 32768).
- `sampling`: optional overrides for `temperature` (0.4), `top_p` (0.8), `top_k` (20),
  `min_p` (0.0) and `repeat_penalty` (1.05). The defaults suit Qwen-style models; check your
  model's card for its recommended values.

Notes:

- Folder paths: relative ones are inside the folder settings.json is in; full paths such as
  `C:/Users/you/Videos/transcripts` also work, on any drive. Use `/` in paths (a single `\`
  breaks the file).
- Command-line options override settings.json for that run.
- A typo in a setting or model field name stops the script with a message saying what to fix.

### Using one specific quantisation

To use one quantisation of a model that has several, import that file into LM Studio as its
own model with a hard link (no extra disk space):

```
lms import "<path to>.gguf" -L --user-repo transcript-report/<name> -y
```

## LM Studio on another computer

To run the model on another computer (for example a desktop with a bigger GPU) and the
script on this one:

1. On the other computer, open LM Studio's server settings (Developer tab), turn on
   **Serve on Local Network**, and start the server.
2. Here, set `"lmstudio_url": "http://<that computer's address>:1234"` in settings.json,
   or pass `--lmstudio-url`.
3. Run `--check`: it should find the server and your model.

If **Require Authentication** is turned on in that LM Studio's server settings, create an API
token there and set `"lmstudio_api_token"` in settings.json (or the `LM_API_TOKEN` environment
variable). The token is kept in plain text in settings.json, so don't share that file.

The server on the other computer isn't started or stopped for you, but the model is still
loaded before the analysis and unloaded after it (unless it was already loaded). The VRAM
check is skipped, as it can only see this computer's GPU. The transcript is sent over plain
HTTP, so only do this on a network you trust.

## Custom prompts

The report's sections and wording come from two built-in prompt files (in the package's
`prompts` folder, [transcript_report/prompts](../transcript_report/prompts)):

- `report_prompt.md`: the instructions and format for the final report.
- `section_notes_prompt.md`: the notes taken from each section of a long video. It must keep
  the line `Output only the notes, in exactly this format:` (merging notes reuses the format
  after it) and the `### Style evidence` and `### Padding` headings (the final report step
  refers to them).

To change them, copy them into a folder of your own (for example `my prompts` next to
settings.json), edit them, and set `"prompts_folder"` to that folder. A file that's missing there falls back to the
built-in one, so you can override just one.

## PDF reports

PDFs are made by printing the report with Microsoft Edge or Google Chrome in headless mode,
so nothing extra needs installing. If the PDF can't be made, the report is saved as Markdown
instead and a warning says why.

## When something goes wrong

- Run `python -m transcript_report --check` first (or `python analyse_video.py --check` from
  a clone).
- **"Input is too long for the context" / "LM Studio ended the reply early":** raise `--ctx`
  (if your VRAM allows), or lower `--chunk-words`. The exact error is in LM Studio's server
  log (Developer tab, or `~/.lmstudio/server-logs`).
- **A long video stopped part-way:** run it again with `--resume`.
- **"LM Studio error 401" or "403":** the server requires an API token; see
  [LM Studio on another computer](#lm-studio-on-another-computer).
- **Slow, and VRAM is nearly full:** close GPU-heavy apps or lower `--ctx`. On Windows, once
  VRAM runs out the GPU falls back to shared system memory, which is much slower.
