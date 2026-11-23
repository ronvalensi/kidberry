# Kidberry

A local tool for parents: transcribe the audio from a small recorder in your child's kindergarten bag
(Hebrew, English, Spanish), find the moments worth listening to, and review them by time.
Everything runs on your own PC; audio never leaves the machine. Only if you turn on the optional AI review is
transcript **text** sent to the AI provider you choose.

> **Important.** Kidberry points you to moments *worth listening to*. It is not evidence and it is often wrong:
> speech recognition mishears words, and the AI's corrections are guesses. Always listen before drawing any
> conclusion. Recording conversations you are not part of may be illegal where you live (in Israel, see the
> Secret Monitoring Law); you are responsible for checking the law and for how you use the recordings.

> Work in progress: an installer, a setup wizard and a Hebrew interface are being built.
> Today it needs Windows, Python 3.11 and ffmpeg. An NVIDIA GPU makes it much faster but is not required.

## Setup (once)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

The first transcription downloads the speech models (~3-5 GB).

Then create `data\settings.json` with your child's name and your recorder's folder (see `kidberry/settings.py`
for every field):
```json
{
  "child": {"name": "נועם", "name_latin": "Noam", "heard_as": ["נועמי"], "maybe_heard_as": [], "heard_as_common_word": []},
  "other_children": [{"name": "Yael", "native": "יעל"}],
  "languages": ["he"],
  "ui_language": "en",
  "recorder": {"path": "E:\RECORD"}
}
```
`data\` is never uploaded or overwritten by updates.

### Languages
Set `languages` to what is spoken in the kindergarten: `"he"` Hebrew, `"en"` English, `"es"` Spanish, `"ru"` Russian.
Only the models these need are downloaded and loaded:

| Languages | Speech models |
|---|---|
| Hebrew only | the Hebrew model (~1.6 GB). No language detection, ~40% faster |
| Hebrew + others | the Hebrew model + Whisper large-v3 (~3 GB), which detects each window's language |
| no Hebrew | Whisper large-v3 only |

`ui_language` (`"en"` or `"he"`) is the language other languages are translated into. Hebrew lines are never
translated. `language_notes` can give the AI a hint per language, e.g. `{"es": "Rioplatense"}`.

## Daily use

### From the page
Start the page (`.\.venv\Scripts\python -m kidberry.server`, then open http://127.0.0.1:8000), plug in the recorder and
click **⬇ Import & process**. Pick the day and the drop-off and pickup times, choose whether to run the AI review, and
check the estimate (time, finish time, AI tokens) before you press **Start**. A progress bar shows the step, the overall
percentage and the time left; it keeps going if you close the browser tab. **Stop** halts it, and **Continue** picks up
where it stopped. Recordings are never deleted from the recorder from the page.

### From the terminal
Plug in the recorder, then:
```powershell
.\.venv\Scripts\python -m kidberry.daily --hours 08:30-15:30
```
It copies new recordings from the recorder (checksum-verified; asks before deleting them from the recorder),
sets the kindergarten hours, shows an estimate and asks before starting (`--yes` skips the question), transcribes,
detects sounds, translates other languages, runs keyword flags and the smart AI review, and prints the day's summary and
patterns. `python -m kidberry.estimate` shows the estimate alone. `--no-ai` skips the AI, `--full` reviews every line,
`--day YYYY-MM-DD` picks the day. Then open http://127.0.0.1:8000 (start it with `python -m kidberry.server`)
and **📅 Days overview** for patterns across days.

### Patterns across days (`python -m kidberry.days`, or the Days page)
Over the last 5 kindergarten days (days with hours set), ignoring flags marked False alarm:
- the same kind of flag within ±30 min of the same time of day on 3+ days
- threats / insults / physical / secrecy / neglect / bad language on 3+ days
- long crying with **no adult heard responding** (AI's answer per crying episode) on 2+ days
- anything you marked **Concerning** happening again on a later day

## Your child's name

Every line where your child's name is said is highlighted and marked 👦. **👦 Every time … is said** (left sidebar)
lists them across all days, and **… only** filters one transcript. Speech recognition often mishears names; list the
ways it writes your child's name in `child.heard_as`. A mishearing that is also an everyday word (e.g. בלי, "without")
goes in `child.heard_as_common_word` and counts as "sounds like it" (👦 ?) only when it sounds like calling the child:
alone, before a comma/!/?, at the end of a line, or after "come", "enough", "no"...
(`python -m kidberry.names` lists them in the terminal.) The smart AI review also covers +/-1 min around each mention
and pays extra attention to how adults talk to your child. List the other children in `other_children` so the AI
does not mistake them for yours.

## Individual steps

1. Copy the day's WAV files into a folder.
2. Transcribe (already-done files are skipped, so you can point it at the parent folder):
   ```powershell
   .\.venv\Scripts\python -m kidberry.transcribe D:\Recordings
   ```
3. Start the review UI and open http://127.0.0.1:8000
   ```powershell
   .\.venv\Scripts\python -m kidberry.server
   ```

In the UI, click any transcript line to hear it (playback starts 3s early). The timeline bar shows where speech
happens. Use "Search all recordings" to find a word across every day.

## GPU or CPU

```powershell
.\.venv\Scripts\python -m kidberry.hardware   # what this PC will use, memory check, time for a day
```

| Profile | When | How it runs | An 8-hour day |
|---|---|---|---|
| `gpu` | NVIDIA GPU with 6 GB+ | float16, full models | under an hour |
| `gpu-small` | NVIDIA GPU with 3-6 GB | compressed (int8_float16), same models | about 1-2 hours |
| `cpu` | no usable GPU (most laptops) | int8 on all cores, lighter general model (`large-v3-turbo`), sounds checked every 10 s | several hours: run it overnight |

On a CPU, Hebrew-only is about twice as fast as several languages (no per-window language detection).
Speeds are measured on each run (`data\speed.json`), so estimates fit the PC after the first day.
Force a profile with `"hardware": {"device": "cpu"}` in `data\settings.json`, or `KIDBERRY_DEVICE=cpu`.

Also:
- **Only the kindergarten hours** (plus 5 min either side) are transcribed and scanned when they are set for the day.
  If the hours are later widened, the file is processed again.
- **Interrupted runs continue** from the last checkpoint (every ~5 min of audio).
- Audio is read from disk a slice at a time, so a 5-hour file needs almost no memory.
- Windows is kept awake while processing; a laptop on battery gets a warning.
- Silence is *not* skipped: on muffled recordings a voice detector misses real speech (tested: ~5% of lines lost).

## How transcription works

1. The audio is boosted (high-pass + dynamic loudness normalization) into `recordings/processed/`. The original is never modified.
2. It is split into ~30s windows, cut at quiet moments.
3. With several languages, each window is Hebrew unless the detector is clearly confident it is one of the other
   chosen languages (forcing a wrong language makes Whisper *translate* or invent). With one language, there is no detection.
4. Hebrew goes to `ivrit-ai/whisper-large-v3-turbo-ct2`, the other languages to `large-v3`.
5. Likely hallucinations ("תודה רבה", Knesset phrases, "thanks for watching", repetition loops) are kept in the DB but tagged
   `filtered`. They are hidden in the UI unless you tick **Show filtered**.

After changing the filter rules in `transcribe.py`, re-apply them in seconds without re-transcribing:
```powershell
.\.venv\Scripts\python -m kidberry.transcribe --refilter
```

## Settings (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `KIDBERRY_MODEL_HE` | `ivrit-ai/whisper-large-v3-turbo-ct2` | Hebrew windows |
| `KIDBERRY_MODEL_EN` | `large-v3` | English windows + language detection |
| `KIDBERRY_ENGLISH_MIN_PROB` | `0.5` | how sure the detector must be to treat a window as English |
| `KIDBERRY_DEVICE` | auto | `cpu` or `cuda` to override the hardware check |
| `KIDBERRY_AI_BACKEND`, `KIDBERRY_AI_MODEL`, `KIDBERRY_AI_DAILY_TOKEN_CAP` | from settings | override the `"ai"` settings |
| `KIDBERRY_PORT` | `8000` | |

## Sound detection (crying, screaming, yelling, impacts)

```powershell
.\.venv\Scripts\python -m kidberry.sounds             # scan new recordings (~2 min per 5 h, GPU, free)
.\.venv\Scripts\python -m kidberry.sounds --redetect  # re-apply thresholds in sounds.py to stored scores
.\.venv\Scripts\python -m kidberry.sounds --stats     # how often each sound occurs
```

- Model: Audio Spectrogram Transformer trained on Google AudioSet (`MIT/ast-finetuned-audioset-10-10-0.4593`),
  10 s windows every 5 s. Scores are stored, so thresholds can be tuned without re-scanning.
- Every detected sound shows on the timeline's bottom strip (orange crying, red screaming, purple yelling, grey impact).
- Flags: crying *episodes* with at least 1.5 min of crying (short crying is normal in a toddler group),
  screaming, yelling, impact sounds. Sound flags are severity 1-3; sound alone doesn't prove anything.

## Translation

```powershell
.\.venv\Scripts\python -m kidberry.translate   # local (Helsinki-NLP OPUS-MT models), free
```
Lines in English, Spanish or Russian are translated into the interface language (`ui_language`), shown as
"EN: …" / "HE: …" under each line and included in search. After the AI review corrects a line, it is re-translated
automatically. The child's name is kept as is. Garbled speech gives a garbled translation: check odd lines by listening.
Translation into Hebrew is rougher than into English.

## Kindergarten hours

```powershell
.\.venv\Scripts\python -m kidberry.analyze --hours 2026-11-03 08:34-15:37
```
Flags and the AI review only cover drop-off to pickup. Lines outside are shown faded.

## Analysis (flags + AI correction)

```powershell
.\.venv\Scripts\python -m kidberry.analyze --day 2026-11-03 # smart AI review: +/-3 min around sound/keyword flags
.\.venv\Scripts\python -m kidberry.analyze --day 2026-11-03 --full   # every line in kindergarten hours (~5x tokens)
.\.venv\Scripts\python -m kidberry.analyze --estimate       # lines/tokens it would use, without running
.\.venv\Scripts\python -m kidberry.analyze --blocks 1       # just one batch of ~60 lines, as a test
.\.venv\Scripts\python -m kidberry.analyze --usage          # tokens used per day
.\.venv\Scripts\python -m kidberry.analyze --keywords-only  # only keyword flags (free, local)
```

- **AI review**: sends transcript *text only* (never audio). For every line it writes a best-guess correction of what
  was actually said, and flags concerning moments with category, severity 1–5, and a reason.
- **Daily cap**: stops cleanly at `daily_token_cap` (default 150,000) tokens per day, or when the provider's usage
  limit is hit. Each batch is saved as soon as it returns, so the next run continues from where it stopped.
  A smart-mode day measured ~15,000 tokens per kindergarten hour.
- **Keyword flags**: Hebrew/English/Spanish/Russian warning phrases in `kidberry/keywords.py`, checked against both the
  raw and corrected text.
- In the UI, mark each flag **Concerning** or **False alarm**. Verdicts are kept when flags are regenerated, and will be
  used to tune the analysis.

### Choosing the AI

The AI review is optional. Choose a provider in `data/settings.json`:

```json
"ai": {"provider": "claude-code", "model": "", "daily_token_cap": 150000, "mode": "smart"}
```

| `provider` | What you need | Cost | Privacy |
|---|---|---|---|
| `claude-code` (default) | [Claude Code](https://claude.com/claude-code) logged in to a Claude Pro/Max plan | your plan's usage limits, no extra charge | consumer terms: turn off "help improve Claude" in claude.ai privacy settings |
| `anthropic` | an API key from console.anthropic.com | pay per token, roughly $1 a day | API data not used for training by default |
| `openai` | an API key from platform.openai.com (a ChatGPT plan does not include it) | pay per token | API data not used for training by default, kept up to 30 days |
| `gemini` | an API key from aistudio.google.com | **free tier**, or pay per token | **on the free tier Google may use the text to improve its products and people may read it** |
| `none` | nothing | free | nothing leaves the PC; keyword and sound flags only |

Check the providers' current terms yourself; they change. API keys go in `data/secrets.json` (never uploaded):
`{"anthropic": "...", "openai": "...", "gemini": "..."}`, or in `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`GEMINI_API_KEY`. `model` empty picks a default (Claude: `claude-opus-5-5`; OpenAI and Gemini: the newest main model
your key can use). Rate limits are handled: busy replies are retried after the wait the provider asks for, and a daily
quota or empty credit stops the run cleanly so the next run continues. The Gemini free tier is paced to one request
every 7 seconds (`"interval_s"` to change it).

Test your choice before a real day: one small made-up batch (a few thousand tokens) that checks the AI corrects a
misheard name, leaves the look-alike everyday word alone, flags a threat and leaves ordinary lines alone:

```powershell
.\.venv\Scripts\python -m kidberry.ai --list    # the providers, and whether a key was found
.\.venv\Scripts\python -m kidberry.ai --test
```

The Claude calls are kept lean: Kidberry's own short instructions, no tools, no MCP, no saved session.

## Privacy and support

Kidberry never sends anything about you or its use anywhere: no telemetry, no crash reports, no update pings. The only
thing that ever leaves the PC is transcript text for the AI review, and only if you turn it on.

If something goes wrong, **Having a problem? Make a support report** (on the page, under Import, or
`python -m kidberry.support`) makes a text file with technical facts only: version, Windows, hardware, measured
speeds, the languages and AI provider chosen, and where recent runs failed (error type and code line, never the
message). It has no audio, transcripts, names, flags, recording dates, file paths, settings or keys. You read it and
send it yourself, if you want to.
- **Keyword flags**: Hebrew/English warning phrases in `kidberry/keywords.py`, checked against both the raw and corrected text.
- In the UI, mark each flag **Concerning** or **False alarm**. Verdicts are kept when flags are regenerated, and will be
  used to tune the analysis.

## Roadmap

- [x] Phase 1: transcription with timestamps + review UI
- [x] Phase 2a: keyword flags (Hebrew/English)
- [x] Phase 3: AI correction + AI risk flags with severity + explanation
- [x] Phase 2b: sound-event detection (screaming, prolonged crying, impacts)
- [x] Spanish-speaking staff: Spanish transcription + local English translation
- [x] Smart AI mode: AI review only around sound/keyword triggers (default; `--full` for every line)
- [x] Days overview, patterns across days, "did an adult respond?" per crying episode, one daily command
- [ ] Weekly AI summary of the week's flags
- [x] Kindergarten languages in settings (Hebrew, English, Spanish, Russian); only the models needed are loaded
- [ ] Choose the languages in the setup wizard
- [x] Choice of AI: your Claude plan, Claude / OpenAI / Gemini API key (incl. Gemini's free tier), or no AI; `--test`
- [x] Support report you read and send yourself (no telemetry)
- [ ] Arabic (Hebrew + Arabic kindergartens): waiting for sample recordings to choose and test a dialect model
- [ ] Later: speaker separation (adult vs child), real clock-time per file

## Credits & licenses

Kidberry downloads these models from Hugging Face on first use; they are not bundled.
- Hebrew speech recognition: [ivrit-ai/whisper-large-v3-turbo-ct2](https://huggingface.co/ivrit-ai/whisper-large-v3-turbo-ct2) by ivrit.ai (Apache-2.0)
- Language detection + other languages: [Whisper large-v3](https://huggingface.co/Systran/faster-whisper-large-v3) by OpenAI, CTranslate2 conversion by Systran (MIT);
  on CPU [large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) (MIT)
- Sound detection: [AST fine-tuned on AudioSet](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593) (BSD-3-Clause)
- Translation: Helsinki-NLP OPUS-MT [es-en](https://huggingface.co/Helsinki-NLP/opus-mt-es-en),
  [en-he](https://huggingface.co/Helsinki-NLP/opus-mt-en-he), [es-he](https://huggingface.co/Helsinki-NLP/opus-mt-es-he),
  [ru-he](https://huggingface.co/Helsinki-NLP/opus-mt-ru-he) (Apache-2.0) and
  [ru-en](https://huggingface.co/Helsinki-NLP/opus-mt-ru-en) (CC-BY-4.0)

## Copyright

© 2026 ronvalensi. All rights reserved. The code is shared here to be read and tried; it may not be copied,
modified or redistributed without permission. (The models listed above keep their own licenses.)
