---
name: video-transcription-audit
description: Install and operate a cross-platform, evidence-preserving audio/video transcription workflow using local Whisper models or SiliconFlow, then audit and correct uncertain Chinese transcripts with timestamps. Use this skill whenever the user asks to transcribe, re-transcribe,校对、纠正、核验、生成字幕, configure Whisper/faster-whisper/MLX Whisper/CUDA for transcription, compare ASR models, or process a local audio/video file even when they do not explicitly ask for an audit.
compatibility: Windows 10/11, macOS, or Linux; Python 3.9+; FFmpeg; optional NVIDIA GPU or SiliconFlow API key.
---

# Video Transcription Audit

Transcribe local audio and video files while preserving raw model evidence. Use a second model only where useful, and never turn a plausible rewrite into a claimed transcription.

`{baseDir}` means the directory containing this `SKILL.md`.

## Boundaries

- Handle local audio/video files.
- Do not download YouTube or other remote media with this skill.
- Do not add translation, speaker diarization, subtitle burning, or content analysis unless the user separately requests it.
- Never include an API key in a command shown to the user, a report, `run.json`, or a saved script.
- Treat every file under `raw/` as immutable evidence.

## First-Run Workflow

1. Run the strict installation check without changing the machine:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" doctor --profile auto --install-check
```

```bash
bash "{baseDir}/scripts/asr.sh" doctor --profile auto --install-check
```

2. If dependencies are missing, explain what the installer will change and obtain approval before installing:

```powershell
pwsh -File "{baseDir}/scripts/setup.ps1" -Profile auto
```

```bash
bash "{baseDir}/scripts/setup.sh" --profile auto
```

Use `-DryRun` or `--dry-run` to inspect commands first. The scripts create a dedicated environment under `ASR_HOME` or the user's home directory.

The installer first validates `ASR_PYTHON`, then `${ASR_HOME}/venv`. If the selected environment is install-ready, it exits without running pip or probing package indexes. Missing or incompatible requirements are repaired individually. Use `-Force` or `--force` only when installed packages are present but broken.

The official PyPI index is the default. Use `-IndexUrl URL` or `--index-url URL` for an explicitly trusted index. Use `-ProbeMirrors` or `--probe-mirrors` to benchmark the bundled HTTPS mirror list only when installation is required. Never add `--trusted-host`.

3. If SiliconFlow is needed, read the key only from `SILICONFLOW_API_KEY`. Ask the user to configure that environment variable outside the skill. Never echo its value.

## Backend Selection

Use `--backend auto` unless the user requests a specific backend.

1. Apple Silicon: `mlx-whisper`.
2. Windows/Linux NVIDIA GPU with at least 4 GB VRAM: `faster-whisper`.
3. No suitable accelerator but `SILICONFLOW_API_KEY` is available: SiliconFlow `SenseVoiceSmall`.
4. Otherwise: `faster-whisper small` on CPU with INT8.

The automatic NVIDIA profile prefers `large-v3/float16` at 8 GB VRAM or more and `large-v3/int8_float16` from 4 GB to 8 GB. CTranslate2 must also report a CUDA device and the requested compute type. If `nvidia-smi` and CTranslate2 disagree, report the mismatch and fall back to SiliconFlow or CPU.

## Transcribe

Run:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" transcribe "INPUT_FILE" `
  --backend auto `
  --language zh `
  --output-dir "OUTPUT_DIR" `
  --audit
```

```bash
bash "{baseDir}/scripts/asr.sh" transcribe "INPUT_FILE" \
  --backend auto \
  --language zh \
  --output-dir "OUTPUT_DIR" \
  --audit
```

The output directory must be new. This prevents accidental replacement of raw evidence.

Expected outputs:

```text
OUTPUT_DIR/
├── 01_转录文本.txt
├── 02_字幕.srt
├── 03_待确认内容.md
└── _审计证据/
    ├── source/
    │   ├── audio_16k.mp3
    │   └── media.json
    ├── raw/
    │   ├── <primary>.json
    │   ├── <primary>.txt
    │   ├── <primary>.srt
    │   ├── <primary>.vtt
    │   └── <primary>.tsv
    ├── audit-clips/
    ├── audit.json
    ├── review/
    │   ├── waveform.u8
    │   ├── waveform.meta.json
    │   └── edits.jsonl
    ├── transcript.audit.md
    └── run.json
```

The three numbered files are the user delivery layer. `_审计证据/` is the
technical evidence layer and normally does not need to be opened.

If the user wants a quick raw transcript, omit `--audit`. The three delivery
files are still created, and `03_待确认内容.md` clearly states that multi-model
audit has not run. Run `prepare-audit` later:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" prepare-audit "OUTPUT_DIR" --secondary auto
```

```bash
bash "{baseDir}/scripts/asr.sh" prepare-audit "OUTPUT_DIR" --secondary auto
```

## Review Visually

Open the local video, subtitles, waveform timeline, and correction controls:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" review "OUTPUT_DIR"
```

```bash
bash "{baseDir}/scripts/asr.sh" review "OUTPUT_DIR"
```

The review server binds only to `127.0.0.1`, selects an available port, and
opens the default browser. Use `--no-open` to print the URL without opening it,
or `--port PORT` to select a loopback port. If the original media moved, use:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" review "OUTPUT_DIR" --media "VIDEO_FILE"
```

The selected media must match the transcription duration. The server references
the original video and never copies it. If the original is missing, it falls
back to `_审计证据/source/audio_16k.mp3`.

The editor supports synchronized playback, subtitle overlay, search, unresolved
filtering, playback speed, cue looping, text edits, and cue start/end changes.
It does not add, remove, split, or merge cues. An unresolved interval remains
unresolved after a text edit until the user explicitly chooses
`已听清并确认`.

WaveSurfer assets are bundled with the Skill and require no runtime network
access. Per-run waveform data is cached under `_审计证据/review/` at 20 peaks
per second, approximately 72 KB per hour. Every successful save appends a small
entry to `edits.jsonl`; raw model evidence is never changed.

## Correct The Transcript

Read [references/correction-policy.md](references/correction-policy.md) before changing `01_转录文本.txt` or `02_字幕.srt`.

For every disputed interval:

1. Compare the primary text, secondary text, word probabilities, surrounding sentences, and the audio timestamp.
2. Correct obvious homophones and known proper nouns only when the evidence supports the correction.
3. Do not choose a phrase merely because it reads better.
4. If evidence remains insufficient, keep `[听不清 HH:MM:SS]`.
5. Record the decision and evidence in `_审计证据/transcript.audit.md`.
6. Edit only `01_转录文本.txt`, `02_字幕.srt`, `03_待确认内容.md`, and the decision column in the audit report. Never edit `_审计证据/raw/`.

## Organize Legacy Runs

Older runs with `run.json`, `raw/`, and `source/` at the root remain readable.
Preview a migration without changing files:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" organize "RUN_DIR" --dry-run
```

```bash
bash "{baseDir}/scripts/asr.sh" organize "RUN_DIR" --dry-run
```

Run the migration only after the legacy run validates:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" organize "RUN_DIR"
```

```bash
bash "{baseDir}/scripts/asr.sh" organize "RUN_DIR"
```

The command stages and validates the new layout before switching. Repeating it
on an organized run succeeds without creating another result directory.

## Validate

Always validate before reporting completion:

```bash
pwsh -File "{baseDir}/scripts/asr.ps1" validate "OUTPUT_DIR"
```

```bash
bash "{baseDir}/scripts/asr.sh" validate "OUTPUT_DIR"
```

Validation checks UTF-8, required files, SRT numbering and timestamps, duration coverage, raw file hashes, and accidental API-key leakage.

Report:

- selected backend/model/device;
- installation readiness, missing requirements, and import or CUDA probe errors;
- output file paths;
- whether timestamps are native or aligned;
- unresolved intervals;
- any fallback or incomplete platform verification.

## Platform Notes

Read [references/platforms.md](references/platforms.md) when installation or GPU loading fails.

- Windows is the fully exercised platform for this skill version.
- macOS and Linux installation paths are covered by dry-run and platform-selection tests but require a real host for final GPU verification.
- SiliconFlow currently accepts `FunAudioLLM/SenseVoiceSmall` and `TeleAI/TeleSpeechASR`, limits a request to one hour and 50 MB, and returns text without native timestamps. The CLI records aligned timestamps as aligned, never native.
