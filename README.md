# Kidberry

A local tool for parents: transcribe the audio from a small recorder in your child's kindergarten bag
(Hebrew, English, Spanish), find the moments worth listening to, and review them by time.
Everything runs on your own PC; audio never leaves the machine. Only if you turn on the optional AI review is
transcript **text** sent to the AI provider you choose.

> **Important.** Kidberry points you to moments *worth listening to*. It is not evidence and it is often wrong:
> speech recognition mishears words, and the AI's corrections are guesses. Always listen before drawing any
> conclusion. Recording conversations you are not part of may be illegal where you live (in Israel, see the
> Secret Monitoring Law); you are responsible for checking the law and for how you use the recordings.

> Work in progress: installer, CPU support, choice of AI provider and a Hebrew interface are being built.
> Today it needs Windows, Python 3.11, ffmpeg and an NVIDIA GPU.

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
  "staff_languages": {"spanish": false},
  "recorder": {"path": "E:\RECORD"}
}
```
`data\` is never uploaded or overwritten by updates.

## Daily use: one command

Plug in the recorder, then:
```powershell
.\.venv\Scripts\python -m kidberry.daily --hours 08:30-15:30
```
It copies new recordings from the recorder (checksum-verified; asks before deleting them from the recorder),
sets the kindergarten hours, transcribes, detects sounds, translates Spanish, runs keyword flags and the smart AI
review, and prints the day's summary and patterns. `--no-ai` skips the AI, `--full` reviews every line,
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

## How transcription works

1. The audio is boosted (high-pass + dynamic loudness normalization) into `recordings/processed/`. The original is never modified.
2. It is split into ~30s windows, cut at quiet moments.
3. Each window is Hebrew unless the detector is clearly confident it is English (forcing English on Hebrew makes Whisper *translate*).
4. Hebrew goes to `ivrit-ai/whisper-large-v3-turbo-ct2`, English to `large-v3`.
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
| `KIDBERRY_DEVICE` | `cuda` | `cpu` if no GPU |
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

## English translation of Spanish

```powershell
.\.venv\Scripts\python -m kidberry.translate   # local (Helsinki-NLP/opus-mt-es-en), free
```
Shown as "EN: …" under each Spanish line and included in search. After the AI review corrects a Spanish
line, it is re-translated automatically. Garbled Spanish gives garbled English: check odd lines by listening.

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

- **AI review** (Claude, `claude-opus-5-5`): sends transcript *text only* (never audio). For every line it writes a best-guess
  correction of what was actually said, and flags concerning moments with category, severity 1–5, and a reason.
- **Runs on your Claude subscription** by default, through the Claude Code CLI (`claude` must be installed and logged in).
  Calls are kept lean: own short instructions, no tools, no MCP, no saved session.
  Measured: ~7,300 tokens per batch of 60 lines, so ~100,000 tokens for a full day.
- **Daily cap**: stops cleanly at `KIDBERRY_AI_DAILY_TOKEN_CAP` (default 150,000) tokens per day, or when the plan's usage
  limit is hit. Each batch is saved as soon as it returns, so the next run continues from where it stopped.
- Optional pay-as-you-go instead: `KIDBERRY_AI_BACKEND=api` plus `ANTHROPIC_API_KEY=sk-ant-...` in `.env` (git-ignored).
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
- [ ] Later: speaker separation (adult vs child), real clock-time per file

## Credits & licenses

Kidberry downloads these models from Hugging Face on first use; they are not bundled.
- Hebrew speech recognition: [ivrit-ai/whisper-large-v3-turbo-ct2](https://huggingface.co/ivrit-ai/whisper-large-v3-turbo-ct2) by ivrit.ai (Apache-2.0)
- Language detection + English: [Whisper large-v3](https://huggingface.co/Systran/faster-whisper-large-v3) by OpenAI, CTranslate2 conversion by Systran (MIT)
- Sound detection: [AST fine-tuned on AudioSet](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593) (BSD-3-Clause)
- Spanish to English: [Helsinki-NLP/opus-mt-es-en](https://huggingface.co/Helsinki-NLP/opus-mt-es-en) (Apache-2.0)

## Copyright

© 2026 ronvalensi. All rights reserved. The code is shared here to be read and tried; it may not be copied,
modified or redistributed without permission. (The models listed above keep their own licenses.)
