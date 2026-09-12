# Jellyfin Direct Play Optimizer

A Python script that recursively prepares video files to be as close as possible to **Direct Play** on Jellyfin, minimizing (or eliminating) server-side transcoding.

---

## 🎯 Motivation

Jellyfin is great, but playback quality depends heavily on compatibility between the file and the client (TV, phone, browser, etc.). When a file isn't compatible, the server has to **transcode in real time**, which:

- Consumes a lot of CPU/GPU;
- Can cause stuttering, buffering, and quality drops;
- Prevents multiple simultaneous streams on modest servers;
- Generates more heat, more power consumption, and more hardware wear.

The goal of this script is to **pre-process an entire library** so files land on a profile Jellyfin can deliver via **Direct Play** (or, at worst, Direct Stream), with as little transcoding as possible.

It is designed for **local, batch use** on large folders — series, movies, and animation — and it avoids reprocessing files that are already ready.

---

## ✨ What the script does

For every video found recursively under the root folder:

1. Probes the file with `ffprobe`.
2. Validates the input (duration, streams, tail readability).
3. Picks the **best conversion mode**:
   - **Remux** — swaps the container only, no re-encoding.
   - **Copy video + convert audio** — reuses the video, adjusts only the audio.
   - **Full re-encode** — when nothing is compatible.
4. Extracts compatible subtitles (PT/EN/forced) into external `.srt` files.
5. Copies external subtitles already present in the source folder.
6. Produces a Direct Play–friendly MP4.
7. Validates the output before accepting it (container, codecs, resolution, HDR, etc.).
8. Writes detailed logs.

---

## 🧠 How it decides the mode

The script always tries the **lightest possible path**, in this order:

### 1. `remux`
When both video **and** audio are already in formats Jellyfin accepts:

- Source container is already MP4 (or compatible);
- Video: H.264, Baseline/Main/High profile, level ≤ 4.0, `yuv420p`, ≤ 1920×1080, no HDR, not interlaced, CFR, ≤ 30 fps;
- Audio: AAC, ≤ 2 channels, 48 kHz.

→ Just repackages. **Fast**, with virtually no quality loss.

### 2. `copy video + convert audio`
When video is compatible but audio is not:

- Video is copied without re-encoding;
- Audio is converted to AAC stereo 48 kHz.

→ Great CPU savings, keeps video quality intact.

### 3. `full re-encode`
When neither video nor audio is compatible:

- Video: re-encoded to H.264 (`libx264`, `medium`, Main profile, level 4.0, `yuv420p`, configurable CRF);
- Applies HDR → SDR tonemapping when needed;
- Applies `yadif` deinterlacing when needed;
- Normalizes VFR → CFR;
- Caps framerate at 30 fps and resolution at 1080p;
- Audio: converted to AAC stereo 48 kHz.

---

## 📦 Output structure

Assuming the root is `/media/movies`:

```
/media/movies/
├── DirectPlay/                  ← optimized video output
│   └── <same folder structure as source>/
│       ├── movie.mp4
│       └── movie.pt.srt
├── Logs/                        ← per-file logs
│   └── <same folder structure as source>/
│       ├── movie.log
│       └── movie.progress
└── directplay_optimizer.log       ← overall run log
```

- Source files are **never modified**.
- Files already converted under `DirectPlay/` are **skipped by default** (use `--overwrite` to reprocess).
- The script ignores its own `DirectPlay/` folder to avoid loops.

---

## 📝 Logs

The script produces **three kinds of logs**, each with a purpose:

### 1. Overall log — `directplay_optimizer.log`
Lives at the root of the input folder. Records the whole run:

- Start and end of processing;
- Every file started, skipped, rejected, or completed;
- Decision taken for each file (remux / copy / re-encode);
- Final summary with decision counts per mode.

Useful for **auditing** and quickly understanding what happened in a large run.

### 2. Per-file log — `Logs/<path>/<name>.log`
Records everything that happened to **one specific file**:

- Input path;
- Chosen mode (`remux`, `copy video + convert audio`, `full re-encode`);
- Full `ffmpeg` command executed (handy for reproducing manually);
- Failure diagnostics (from `ffprobe`, `ffmpeg`, or validation);
- Total conversion time;
- Reasons for input or output rejection.

This is the most useful log when something goes wrong — the full `ffmpeg` command is right there.

### 3. Progress file — `Logs/<path>/<name>.progress`
A temporary file written by `ffmpeg` itself via `-progress`. It's read in real time to display:

- Percent complete;
- Processed time;
- Speed (`speed`);
- Duplicated frames (`dup`);
- Dropped frames (`drop`).

It's deleted automatically at the end of each conversion.

---

## ✅ Validations

Before accepting a file, the script checks both **input** and **output**.

### Input validation
- Container duration present and valid;
- At least one valid video stream;
- Video duration consistent with the container;
- Reads the last 5 seconds of each video and audio stream (detects truncated or tail-corrupted files).

### Output validation
- Container is MP4;
- Exactly 1 video stream;
- Video is H.264, `yuv420p`;
- Resolution ≤ 1920×1080;
- Framerate ≤ 30 fps;
- No HDR metadata;
- Not interlaced;
- Audio is AAC, ≤ 2 channels, 48 kHz;
- Minimum size of 1 MiB (avoids empty/truncated outputs).

If the output fails validation, it is **discarded** and the reason is logged.

---

## 🚀 Usage

### Requirements
- Python 3.9+;
- `ffmpeg` and `ffprobe` available on `PATH`.

### Basic command
```bash
python otimizar_recursivo.py /path/to/library
```

### Options

| Option | Description |
|---|---|
| `path` | Root input folder (default: `.`). |
| `--crf N` | H.264 CRF, from 0 to 51 (default: `20`). |
| `--analyze` | Analyze and show the decision only, without converting. |
| `--full` | Force **full re-encode** for every file. |
| `--overwrite` | Reprocess files that already have output under `DirectPlay/`. |

`--analyze` and `--full` are mutually exclusive.

### Examples

**Just analyze the library before touching anything:**
```bash
python otimizar_recursivo.py /media/movies --analyze
```

**Run in hybrid mode (automatic remux / copy / re-encode):**
```bash
python otimizar_recursivo.py /media/shows
```

**Force full re-encode with a higher CRF (smaller files):**
```bash
python otimizar_recursivo.py /media/shows --full --crf 23
```

**Reprocess everything, overwriting old outputs:**
```bash
python otimizar_recursivo.py /media/shows --overwrite
```

---

## 🎬 Subtitles

- **Embedded** subtitles in PT, EN, or flagged as *forced* are extracted as `.srt` next to the MP4, named like:
  ```
  movie.pt.srt
  movie.en.srt
  movie.pt.forced.srt
  movie.pt.default.srt
  ```
- **External** subtitles in the source folder are only copied if their name indicates:
  - PT/EN language (`pt`, `pt-BR`, `por`, `portuguese`, `en`, `eng`, `english`, …); or
  - Being *forced*.
- No subtitle is embedded into the MP4: the goal is Direct Play + external subtitle.

---

## ⚠️ Known limitations

- The target profile is **conservative** (H.264 Main @ 4.0, 1080p, 30 fps, AAC stereo). This maximizes compatibility but may reduce quality for very high-quality sources.
- **Doesn't handle HDR10+ / dynamic Dolby Vision perfectly**: applies static Mobius tonemapping to SDR.
- **Doesn't handle multiple angles, interactive tracks, or complex chapters**.
- "Animation" detection is based on the **filename** (simple heuristic).
- No parallelism: processes **one file at a time** to avoid saturating the CPU.

---

## 📄 License

This project is released under the **MIT License** — one of the most permissive licenses available. You can use, copy, modify, merge, publish, distribute, sublicense, and **sell** copies of the software, as long as you keep the copyright notice and license text.

If you want to waive **all** rights and release it into the public domain, an even more permissive alternative is the **Unlicense** or **CC0 1.0**. Either works; MIT is the most widely recognized.

See the `LICENSE` file for the full text.
