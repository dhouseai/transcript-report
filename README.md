# transcript-report

Turn a YouTube video's transcript into an honest, practical briefing, using a local model
on your own GPU. Nothing leaves your machine.

You give it a transcript; it gives you a report with:

- a **TL;DR** and executive summary, and who the video is for
- the **actionable points**, prioritised by impact and effort, and a phased **implementation plan**
- an **accuracy check** of the main claims (✅ / ⚠️ / ❌ / ❓), and which ones to verify yourself
- **critical analysis**: bias and sponsorships, weak arguments, what's missing, speaker credibility
- a **glossary** and the **resources** mentioned
- a **content density score** (how much is filler) and an **AI-generated likelihood** score

See a full example: [examples/sample_report.md](examples/sample_report.md).

> **TL;DR:** A 24-minute SaaS growth video delivers five solid early-stage tips in roughly
> 5 minutes, buried under ~80% filler, sponsor reads, and recycled loops.

Reports are saved as Markdown or PDF. Videos of any length work: long ones are split into
sections, summarised, and combined.

## Requirements

- **[LM Studio](https://lmstudio.ai)** (free), which runs the model.
- **A GPU with roughly 8 GB of VRAM or more.** NVIDIA is what it's tested on; anything LM
  Studio supports should work.
- **Python 3.10 or newer.** It has no dependencies: only the standard library is used.
- **Microsoft Edge or Google Chrome**, only for PDF reports. Edge comes with Windows.

It's developed and used on Windows 11. The code also supports macOS and Linux (LM Studio
runs there too), but those haven't been tried on real videos yet. Reports of how it goes
are welcome.

## Quick start

1. **Install LM Studio** and open it once, so its `lms` command-line tool is set up.
2. **Download a model** in LM Studio (see [Choosing a model](#choosing-a-model)), then run
   `lms ls` to see its key.
3. **Install transcript-report** (needs [Git](https://git-scm.com); or download the ZIP from
   GitHub and run `python -m pip install <unzipped folder>`):
   ```
   python -m pip install git+https://github.com/<owner>/transcript-report
   ```
4. **Create your settings and folders:**
   ```
   python -m transcript_report --init
   ```
   This creates a `transcript-report` folder in your home folder (for example
   `C:\Users\you\transcript-report`) with `settings.json`, a `transcripts` folder and a
   `reports` folder. In `settings.json`, replace `REPLACE-with-a-key-from-lms-ls` with your
   model's key, and set `"ctx"` (see the table below).
5. **Check the setup:**
   ```
   python -m transcript_report --check
   ```
   It tests the settings, LM Studio, the model and the PDF browser, and says what to fix.
6. **Add a transcript and run it.** Paste a transcript into a `.txt` file in the
   `transcripts` folder (see [Getting a transcript](docs/usage.md#getting-a-transcript)),
   then:
   ```
   python -m transcript_report "My video.txt" --title "Video title - Channel"
   ```
   The report is saved in `reports/My video/`, and the transcript moves to
   `transcripts/done/`.

> **The `transcript-report` command.** pip also installs a `transcript-report` command, so
> you can type `transcript-report "My video.txt"` instead. On Windows with **Smart App
> Control** turned on, that command is blocked, because the small `.exe` pip makes for it
> isn't signed. `python -m transcript_report` does exactly the same thing and always works.

**Running from a clone instead.** `git clone` the repository and use
`python analyse_video.py ...` in that folder, with no install. Settings and the transcripts
and reports folders are then kept in the project folder itself (`--init` creates them there).

**Using another folder.** To keep settings, transcripts and reports somewhere else, set the
`TRANSCRIPT_REPORT_HOME` environment variable to that folder.

**LM Studio on another computer.** Turn on *Serve on Local Network* in LM Studio's server
settings on that computer, then set `"server_url"` in `settings.json` (for example
`"http://192.168.1.20:1234"`) or pass `--server-url`. The server there isn't started or
stopped for you, but the model is still loaded and unloaded. If that server requires an API token,
set `"api_token"` too.

## Everyday use

```
python -m transcript_report "My video.txt"          analyse one transcript
python -m transcript_report "My video.txt" --pdf    ... and save the report as a PDF
python -m transcript_report --all                   analyse everything waiting in the folder
python -m transcript_report "Long video.txt" --resume   continue a long run that stopped
```

Transcripts can be text copied from YouTube's **Show transcript** panel, or `.srt` / `.vtt`
subtitle files (for example YouTube's auto-generated captions, downloaded with yt-dlp).

All the options, settings, custom prompts and troubleshooting are in
**[docs/usage.md](docs/usage.md)**.

## Choosing a model

Pick an instruction-tuned model of 7–14B parameters in GGUF format, at a quantisation that
leaves room in VRAM for the context. Longer contexts mean fewer, bigger sections for long
videos, which gives better reports. Rough starting points:

| VRAM | Model size and quantisation | `ctx` to try |
|---|---|---|
| 8 GB | 7–9B at Q4_K_M | 16384 |
| 12 GB | 8–9B at Q6_K (tested: Qwen3.5 9B Q6_K uses about 9.1 GB at 65536) | 32768–65536 |
| 16 GB | 12–14B at Q4_K_M–Q5_K_M | 32768 |
| 24 GB | 14B at Q8_0, or 24–32B at Q4_K_M | 32768–65536 |

These are estimates, apart from the tested row: memory use depends on the model's
architecture. After the model loads, the script prints the VRAM in use and warns if less than
2 GB is free. If it does, lower `ctx`, because once VRAM runs out the GPU falls back to much
slower shared memory.

The default sampling settings suit Qwen-style models. For other models, set the values from
the model's card under `"sampling"` in `settings.json`.

## How it works

```mermaid
flowchart LR
    T[Transcript] --> C[Clean up<br>timestamps, captions]
    C --> F{Fits the<br>context?}
    F -- yes --> R[Write the report]
    F -- no --> S[Split into sections]
    S --> N[Notes for each section<br>saved as they're done]
    N --> M[Merge notes<br>until they fit]
    M --> R
    R --> O[Markdown or PDF]
```

- The script starts LM Studio's server if needed, loads the model with one request slot and
  flash attention, and unloads it at the end so your VRAM is free again. If you already had
  the model loaded, it uses that copy and leaves it loaded.
- Token counts are estimated on the high side, because LM Studio's API has no tokenizer.
- PDFs are made by printing the report with Edge or Chrome in headless mode, so no PDF
  library is needed.

## Limitations

- **The accuracy check is offline.** The model only knows what it learnt in training, so
  recent or very specific claims are marked ❓ Can't verify, and the report lists claims to
  check yourself. Treat its verdicts as a starting point, not a fact-check.
- **The report is only as good as the model and the transcript.** Small models miss nuance,
  and auto-generated captions have no punctuation and contain misheard words (the report
  notes these under Confidence Notes).
- The prompts are written for English-language tech and business videos.

## Privacy

Everything runs locally: the transcript, the model and the report stay on your machine. The
script only talks to LM Studio, on `127.0.0.1` unless you set `server_url` to another
computer. In that case the transcript is sent there over plain HTTP, so only do that on a
network you trust.

## Development

```
python -m unittest discover -s tests -t .
```

The tests need no GPU or LM Studio: the model is replaced by a fake. They run on GitHub
Actions on Windows, macOS and Linux with Python 3.10–3.14.

Lint with `ruff check .` (settings in `pyproject.toml`). See
[CONTRIBUTING.md](CONTRIBUTING.md) for more.

The code is in `transcript_report/`, one job per module; the docstring in
`transcript_report/__init__.py` has a map. `python -m pip install -e .` installs it from your
clone so that changes take effect straight away (settings stay in the project folder).

## License

[MIT](LICENSE)
