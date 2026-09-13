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

On servers where transcoding is **disabled**, there is no "at worst": a file that doesn't match the client's profile won't play at all. The targeting rules exist precisely for that scenario — see the **Target profile and transcoding** section below.

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

## ⚠️ Target profile and transcoding

The script targets servers running with **transcoding disabled**. With transcoding off, Jellyfin cannot fall back to a transcoded or remuxed stream — anything outside the client's declared profile simply **does not play**. There is no degradation path.

That is why the target is deliberately conservative, and every rule exists to maximize the chance of Direct Play on *any* client:

| Dimension | Target | Why |
| --- | --- | --- |
| Container | MP4 (`+faststart`) | Most widely direct-playable container |
| Video | H.264, Baseline/Constrained Baseline/Main/High, level ≤ 4.1, `avc1`, `yuv420p` | Broadly supported; level 4.1 covers 1080p30 |
| Resolution / fps | ≤ 1920×1080, ≤ 30 fps, progressive, SDR | Avoids HDR / interlaced / framerate incompatibilities |
| Audio | AAC-LC, **≤ 2 channels**, 48 kHz | See note below |
| Subtitles | External text `.srt` only (no embedded streams) | Text subtitles don't require transcoding |

**Why audio is forced to stereo:** Jellyfin decides Direct Play from the **profile declared by the client**, and that profile caps the number of audio channels. The official web client, for instance, limits audio channels to the machine's physical channel count — typically 2 on a stereo setup — regardless of what the file contains. A 5.1 track would therefore fail to play on those clients. The downmix to stereo is intentional: it is the format that direct-plays everywhere.

If every client you use declares 6 channels (native TV/box apps usually do), preserving 5.1 could be revisited — but it was deliberately left out because it trades "plays everywhere" for "plays on surround-capable clients only".

---

## 🧠 How it decides the mode

The script always tries the **lightest possible path**, in this order:

### 1. `remux`

When both video **and** audio are already in formats Jellyfin accepts:

- Source container is already MP4 (or compatible);
- Video: H.264, Baseline/Constrained Baseline/Main/High profile, level ≤ 4.1, `yuv420p`, ≤ 1920×1080, no HDR, not interlaced, CFR, ≤ 30 fps;
- Audio: AAC, ≤ 2 channels, 48 kHz.

→ Just repackages. **Fast**, with virtually no quality loss.

### 2. `copy video + convert audio`

When video is compatible but audio is not:

- Video is copied without re-encoding;
- Audio is converted to AAC stereo 48 kHz.

→ Great CPU savings, keeps video quality intact.

### 3. `full re-encode`

When neither video nor audio is compatible:

- Video: re-encoded to H.264, High profile, level 4.1, `yuv420p`, configurable CRF:
  - **CPU (default)** — `libx264` with `-preset fast` and `-tune animation` (filename heuristic) or `film`, using `-crf N`;
  - **GPU (opt-in)** — `h264_nvenc` with `-preset p5 -tune hq -rc vbr -cq N -b:v 0`, enabled by `--encoder nvenc`;
- Applies HDR → SDR tonemapping when needed;
- Applies `yadif` deinterlacing when needed;
- Normalizes VFR → CFR;
- Caps framerate at 30 fps and resolution at 1080p;
- Audio: converted to AAC stereo 48 kHz.

> **No automatic fallback:** if `--encoder nvenc` is used and the GPU encode fails (missing driver, no NVENC-capable device, `ffmpeg` built without `h264_nvenc`, unsupported NVENC options, etc.), the file is reported as **failed**. The script detects and explains the reason but does **not** silently switch to CPU — re-run with `--encoder libx264` (or simply omit `--encoder`) to use the CPU.

> **GPU only accelerates the encode:** filters (HDR tonemapping, `yadif`, scaling) still run on the CPU, so the speed-up is partial on files that need them.

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
├── directplay_optimizer.log       ← overall run log
├── directplay_falhas.txt          ← failures of the last run (only when there are any)
└── directplay_auditoria.txt       ← risk conditions found by the last --audit run
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
- Chosen mode (`remux`, `copy video + convert audio`, `full re-encode`); when GPU encoding is used, the mode line is annotated as `Modo: recodificacao (nvenc)`;
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
- Video profile is Baseline, Constrained Baseline, Main or High;
- Video level ≤ 4.1;
- Video codec tag is `avc1`;
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
- For GPU encoding (`--encoder nvenc`): an NVIDIA GPU with a recent driver **and** an `ffmpeg` build that includes `h264_nvenc`.

### Basic command

```bash
python jellyfin_direct_play_optimizer.py /path/to/library
```

The default encoder is the CPU (`libx264`). Pass `--encoder nvenc` to encode on the GPU.

### Options

| Option | Description |
| --- | --- |
| `path` | Root input folder (default: `.`). |
| `--crf N` | H.264 CRF, from 0 to 51 (default: `20`). |
| `--analyze` | Analyze and show the decision only, without converting. |
| `--full` | Force **full re-encode** for every file. |
| `--audit` | Scan the library and report the risk conditions found, **without converting anything**. |
| `--overwrite` | Reprocess files that already have output under `DirectPlay/`. |
| `--retry-failed` | Reprocess only the files that failed in the previous run. |
| `--encoder {libx264,nvenc}` | Video encoder: `libx264` (CPU, default) or `nvenc` (NVIDIA GPU). |

`--analyze`, `--full`, and `--audit` are mutually exclusive. `--encoder` applies only to **full re-encode**; `remux` and `copy video + convert audio` never re-encode the video file.

### Audit mode (`--audit`)

A **read-only scan**: it runs `ffprobe` on every source and reports what stands in the way of Direct Play. Nothing is converted, no output folder is created, and there is no input validation, so it is fast and safe to run over a whole library.

```bash
python jellyfin_direct_play_optimizer.py /media/movies --audit
```

The console shows, and `directplay_auditoria.txt` records, in this order:

- **What the script would do** with each file: `nothing (already compatible)`, `copy video + convert audio`, or `full re-encode` — so you know how much work a real run would do;
- **Risk conditions** found in the sources, with the number of affected files and up to 10 example paths each;
- Files that **could not be probed at all** (corrupt or unsupported).

The conditions are not mutually exclusive — a single file usually triggers several:

| Condition | Why it matters |
| --- | --- |
| container not MP4 | A remux solves it, no re-encoding needed. |
| video not H.264 | Requires a full re-encode. |
| video profile outside Baseline / Constrained Baseline / Main / High | Rejected by the clients. |
| video level above 4.1 | Rejected by the clients. |
| pixel format other than `yuv420p` | Rejected by several clients. |
| resolution above 1080p | Above the target profile. |
| frame rate above 30 fps | Above the target profile. |
| variable frame rate (VFR) | Audio/video desync on Direct Play. |
| interlaced video | Requires deinterlacing (`yadif`). |
| HDR / Dolby Vision | Requires tone mapping to reach the SDR target. |
| anamorphic video (SAR other than 1:1) | Clients that ignore the sample aspect ratio show wrong proportions. **Reported but not fixed** (see *Known limitations*). |
| audio not AAC | Re-encoded to AAC-LC. |
| audio with more than 2 channels | Downmixed to stereo (intentional). |
| audio sample rate other than 48 kHz | Re-encoded to 48 kHz. |
| HE-AAC audio profile | The web client declares `NotEquals AudioProfile HE-AAC`. **Reported but not fixed yet** (see *Known limitations*). |
| image-based subtitles (PGS/VobSub) | Unusable without transcoding, so they are dropped. |

Most of these conditions are handled automatically by the conversion modes; the two marked as *not fixed* only mean the script keeps the source as is, and Jellyfin may fall back to transcoding for them if the permission is enabled.

### Run summary and retrying failures

When a run ends, the script prints a summary with the outcome of every file — converted, skipped (output already existed), analyzed, rejected in validation, and failed — and writes the same counts to the overall log.

If anything failed, up to 20 relative paths are printed and all of them are saved to `directplay_falhas.txt` at the root of the input folder, so they can be retried with:

```bash
python jellyfin_direct_play_optimizer.py /media/movies --retry-failed
```

The file is rewritten on every run that has failures and removed when a run finishes with none, so `--retry-failed` always points at the most recent failures. `--analyze` never removes it (handy to inspect the problem files first).

### Examples

**Audit the library first — nothing is converted, the risks are just listed:**

```bash
python jellyfin_direct_play_optimizer.py /media/movies --audit
```

**Just analyze the library before touching anything:**

```bash
python jellyfin_direct_play_optimizer.py /media/movies --analyze
```

**Run in hybrid mode (automatic remux / copy / re-encode on CPU):**

```bash
python jellyfin_direct_play_optimizer.py /media/shows
```

**Force full re-encode with a higher CRF (smaller files):**

```bash
python jellyfin_direct_play_optimizer.py /media/shows --full --crf 23
```

**Force full re-encode on the GPU (NVENC):**

```bash
python jellyfin_direct_play_optimizer.py /media/shows --full --encoder nvenc
```

- **Anamorphic video (SAR other than 1:1) is copied as is**: it is treated as Direct-Play compatible, but clients that ignore the sample aspect ratio may show wrong proportions. `--audit` flags these files so you can decide case by case.
- **HE-AAC audio is copied instead of re-encoded**: an AAC track with ≤ 2 channels and 48 kHz is copied even when its profile is `HE-AAC`, which the web client rejects (`NotEquals AudioProfile HE-AAC`). `--audit` reports it as `audio_profile`.
**Force full re-encode explicitly on the CPU:**

```bash
python jellyfin_direct_play_optimizer.py /media/shows --full --encoder libx264
```

**Reprocess everything, overwriting old outputs:**

```bash
python jellyfin_direct_play_optimizer.py /media/shows --overwrite
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

- The target profile is **conservative** (H.264 High @ 4.1, 1080p, 30 fps, AAC stereo). This maximizes compatibility but may reduce quality for very high-quality sources.
- **Audio is always downmixed to AAC stereo** (≤ 2 channels). Multi-channel sources lose their surround layout on purpose: with transcoding disabled, a 5.1 track would not play on clients that cap audio at 2 channels. 5.1 output is not an option today.
- **Image-based subtitles (PGS/VobSub) are not usable**: burning them in would require transcoding. Only text subtitles are extracted, as external `.srt`.
- **Doesn't handle HDR10+ / dynamic Dolby Vision perfectly**: applies static Mobius tonemapping to SDR.
- **Doesn't handle multiple angles, interactive tracks, or complex chapters**.
- "Animation" detection is based on the **filename** (simple heuristic).
- No parallelism: processes **one file at a time** to avoid saturating the CPU (with `--encoder nvenc`, the encode itself runs on the GPU).
- GPU encoding (`--encoder nvenc`) has no automatic CPU fallback: if it fails, the file is reported as failed and must be re-run with `--encoder libx264`.

---

## 📄 License

This project is released under the **MIT License** — one of the most permissive licenses available. You can use, copy, modify, merge, publish, distribute, sublicense, and **sell** copies of the software, as long as you keep the copyright notice and license text.

If you want to waive **all** rights and release it into the public domain, an even more permissive alternative is the **Unlicense** or **CC0 1.0**. Either works; MIT is the most widely recognized.

See the `LICENSE` file for the full text.
