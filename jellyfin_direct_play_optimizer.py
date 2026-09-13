from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mkv", ".avi", ".mp4", ".mov", ".wmv", ".ts"}
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "mov_text", "ass", "ssa", "webvtt", "tx3g"}
PORTUGUESE_LANGUAGES = {"pt", "pt-br", "pt_pt", "por", "portuguese"}
ENGLISH_LANGUAGES = {"en", "en-us", "en-gb", "eng", "english"}
COMMENTARY_TERMS = {"commentary", "comentario", "comentários", "descriptive", "audiodescription", "audio description"}
LANGUAGE_CODES = {
    "pt": "pt",
    "pt-br": "pt",
    "pt-pt": "pt",
    "pt_pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "eng": "en",
    "english": "en",
}


DIRECT_PLAY_VIDEO_PROFILES = {"baseline", "constrained baseline", "main", "high"}
RETRYABLE_OUTCOMES = {"failed", "rejected"}
DIRECT_PLAY_AUDIO_PROFILES = {"lc", ""}
IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
AUDIT_CONDITION_LABELS = {
    "container": "container outside MP4 (a remux solves it)",
    "video_codec": "video is not H.264",
    "video_profile": "video profile outside the accepted set",
    "video_level": "video level above 4.1",
    "video_pix_fmt": "pixel format other than yuv420p",
    "video_resolution": "resolution above 1080p",
    "video_fps": "frame rate above 30 fps",
    "video_vfr": "variable frame rate (VFR)",
    "video_interlaced": "interlaced video",
    "video_hdr": "HDR / Dolby Vision",
    "video_anamorphic": "anamorphic video (SAR other than 1:1)",
    "audio_codec": "audio is not AAC",
    "audio_channels": "audio with more than 2 channels",
    "audio_sample_rate": "audio sample rate other than 48 kHz",
    "audio_profile": "HE-AAC audio (profile other than LC)",
    "image_subtitles": "image-based subtitles (PGS/VobSub)",
}
AUDIT_MAX_EXAMPLES = 10


def run_probe(source: Path) -> dict[str, Any] | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(source),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not result.stdout.strip():
        print(f"Failed to probe: {source}", file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        print(f"Invalid ffprobe JSON for {source}: {error}", file=sys.stderr)
        return None


def parse_duration(value: Any) -> float:
    try:
        duration = float(value)
        return duration if duration > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def write_log(log_file: Path, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def format_seconds(value: str) -> str:
    try:
        total = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "--:--:--"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def validate_input(source: Path, probe: dict[str, Any]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    format_duration = parse_duration(probe.get("format", {}).get("duration"))
    if format_duration <= 0:
        problems.append("missing or invalid duration")

    video = first_video_stream(probe)
    if video is None:
        problems.append("no valid video stream")
    else:
        video_duration = parse_duration(video.get("duration"))
        if video_duration > 0 and format_duration > 0:
            difference = abs(format_duration - video_duration)
            if difference > max(2.0, format_duration * 0.05):
                problems.append("video duration differs significantly from the container")

    if problems:
        return False, problems

    streams_to_check = [(video, "video")]
    streams_to_check.extend(
        (stream, "audio")
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == "audio"
    )
    for stream, label in streams_to_check:
        tail_check = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-sseof",
            "-5",
            "-i",
            str(source),
            "-map",
            f"0:{stream['index']}",
            "-t",
            "5",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(tail_check, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            problems.append(f"failed to read the end of the {label} stream")
            if result.stderr.strip():
                print(f"  -> Diagnostics: {result.stderr.strip()}", file=sys.stderr)
    return not problems, problems


def first_video_stream(probe: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            stream
            for stream in probe.get("streams", [])
            if stream.get("codec_type") == "video" and stream.get("codec_name") != "mjpeg"
        ),
        None,
    )


def is_hdr(video: dict[str, Any]) -> bool:
    transfer = str(video.get("color_transfer", "")).lower()
    side_data = json.dumps(video.get("side_data_list", []), ensure_ascii=False).lower()
    hdr_side_data = (
        "dolby vision" in side_data
        or "dovi" in side_data
        or "hdr10+" in side_data
        or "dynamic hdr" in side_data
        or "mastering display" in side_data
        or "content light level" in side_data
    )
    return (
        transfer in {"smpte2084", "arib-std-b67"}
        or hdr_side_data
    )


def is_full_range(video: dict[str, Any]) -> bool:
    return str(video.get("color_range", "")).lower() in {"pc", "jpeg", "full"}


def is_interlaced(video: dict[str, Any]) -> bool:
    return str(video.get("field_order", "")).lower() in {"tt", "bb", "tb", "bt"}


def parse_frame_rate(video: dict[str, Any], field: str = "r_frame_rate") -> float:
    value = video.get(field, "")
    try:
        numerator, denominator = str(value).split("/", 1)
        return float(numerator) / float(denominator) if float(denominator) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def is_variable_frame_rate(video: dict[str, Any]) -> bool:
    nominal = parse_frame_rate(video, "r_frame_rate")
    average = parse_frame_rate(video, "avg_frame_rate")
    if nominal <= 0 or average <= 0:
        return False
    return abs(nominal - average) > max(0.1, average * 0.02)


def target_frame_rate(video: dict[str, Any]) -> str | None:
    nominal = parse_frame_rate(video, "r_frame_rate")
    average = parse_frame_rate(video, "avg_frame_rate")
    if is_variable_frame_rate(video) and average > 0:
        return f"{min(30.0, average):.6f}".rstrip("0").rstrip(".")
    if nominal > 30:
        return "30"
    return None


def validate_output(output: Path) -> tuple[bool, list[str]]:
    probe = run_probe(output)
    if probe is None:
        return False, ["ffprobe could not read the output"]

    problems: list[str] = []
    format_name = str(probe.get("format", {}).get("format_name", "")).lower()
    if "mp4" not in format_name:
        problems.append(f"invalid container: {format_name or 'unknown'}")

    videos = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    if len(videos) != 1:
        problems.append(f"invalid video stream count: {len(videos)}")
    else:
        video = videos[0]
        if str(video.get("codec_name", "")).lower() != "h264":
            problems.append(f"invalid video codec: {video.get('codec_name', 'unknown')}")
        if str(video.get("profile", "")).lower() not in DIRECT_PLAY_VIDEO_PROFILES:
            problems.append(f"invalid video profile: {video.get('profile', 'unknown')}")
        if int(video.get("level", 0) or 0) > 41:
            problems.append(f"video level above the limit: {video.get('level', 'unknown')}")
        if str(video.get("codec_tag_string", "")).lower() != "avc1":
            problems.append(f"invalid video codec tag: {video.get('codec_tag_string', 'unknown')}")
        if str(video.get("pix_fmt", "")).lower() != "yuv420p":
            problems.append(f"invalid pixel format: {video.get('pix_fmt', 'unknown')}")
        if int(video.get("width", 0)) > 1920 or int(video.get("height", 0)) > 1080:
            problems.append(f"resolution above the limit: {video.get('width')}x{video.get('height')}")
        if parse_frame_rate(video) > 30.001 or parse_frame_rate(video, "avg_frame_rate") > 30.001:
            problems.append("frame rate above 30 fps")
        if is_hdr(video):
            problems.append("output still carries HDR metadata")
        if str(video.get("field_order", "progressive")).lower() not in {"progressive", "unknown", ""}:
            problems.append("output is still interlaced")

    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        if str(stream.get("codec_name", "")).lower() != "aac":
            problems.append(f"invalid audio codec: {stream.get('codec_name', 'unknown')}")
        if str(stream.get("profile") or "").lower() not in DIRECT_PLAY_AUDIO_PROFILES:
            problems.append(f"invalid audio profile: {stream.get('profile') or 'unknown'}")
        if int(stream.get("channels", 0)) > 2:
            problems.append("audio with more than two channels")
        if str(stream.get("sample_rate", "")) != "48000":
            problems.append(f"invalid audio sample rate: {stream.get('sample_rate', 'unknown')}")

    return not problems, problems


def is_remux_compatible(probe: dict[str, Any], video: dict[str, Any], audio_streams: list[dict[str, Any]]) -> bool:
    format_name = str(probe.get("format", {}).get("format_name", "")).lower()
    if "mp4" not in format_name or not is_video_copy_compatible(video):
        return False
    return all(
        str(stream.get("codec_name", "")).lower() == "aac"
        and int(stream.get("channels", 0)) <= 2
        and str(stream.get("sample_rate", "")) == "48000"
        and str(stream.get("profile") or "").lower() in DIRECT_PLAY_AUDIO_PROFILES
        for stream in audio_streams
    )


def is_video_copy_compatible(video: dict[str, Any]) -> bool:
    profile = str(video.get("profile", "")).lower()
    level = int(video.get("level", 0) or 0)
    return (
        str(video.get("codec_name", "")).lower() == "h264"
        and profile in DIRECT_PLAY_VIDEO_PROFILES
        and level <= 41
        and str(video.get("pix_fmt", "")).lower() == "yuv420p"
        and int(video.get("width", 0)) <= 1920
        and int(video.get("height", 0)) <= 1080
        and not is_hdr(video)
        and not is_interlaced(video)
        and not is_variable_frame_rate(video)
        and parse_frame_rate(video) <= 30.001
        and parse_frame_rate(video, "avg_frame_rate") <= 30.001
    )


def is_animation(source: Path) -> bool:
    return any(term in str(source).lower() for term in ("anime", "dragon_ball", "cartoon", "animacao", "desenho"))


def stream_language(stream: dict[str, Any]) -> str:
    language = str(stream.get("tags", {}).get("language", "und")).lower().replace("_", "-")
    return language or "und"


def normalized_language(stream: dict[str, Any]) -> str:
    language = stream_language(stream)
    return LANGUAGE_CODES.get(language, language if re.fullmatch(r"[a-z]{3}", language) else "und")


def is_portuguese(stream: dict[str, Any]) -> bool:
    return stream_language(stream) in PORTUGUESE_LANGUAGES


def is_english(stream: dict[str, Any]) -> bool:
    return stream_language(stream) in ENGLISH_LANGUAGES


def stream_title(stream: dict[str, Any]) -> str:
    title = str(stream.get("tags", {}).get("title", ""))
    return re.sub(r"\s+", " ", title).strip()


def is_commentary(stream: dict[str, Any]) -> bool:
    title = stream_title(stream).lower()
    return any(term in title for term in COMMENTARY_TERMS)


def is_forced(stream: dict[str, Any]) -> bool:
    title = stream_title(stream).lower()
    return stream.get("disposition", {}).get("forced") == 1 or "forced" in title or "forçada" in title


def is_default(stream: dict[str, Any]) -> bool:
    return stream.get("disposition", {}).get("default") == 1


def subtitle_suffix(stream: dict[str, Any], is_default_subtitle: bool) -> str:
    language = normalized_language(stream)
    title = str(stream.get("tags", {}).get("title", ""))
    suffix = f".{language}"
    if is_forced(stream):
        suffix += ".forced"
    elif is_default_subtitle:
        suffix += ".default"
    return suffix


def extract_subtitles(source: Path, destination: Path, base_name: str, streams: list[dict[str, Any]]) -> None:
    text_streams = [
        stream
        for stream in streams
        if str(stream.get("codec_name", "")).lower() in TEXT_SUBTITLE_CODECS
        and (is_portuguese(stream) or is_english(stream) or is_forced(stream))
    ]
    normal_subtitles = [stream for stream in text_streams if not is_forced(stream)]
    preferred_default = next((stream for stream in normal_subtitles if is_portuguese(stream)), None)
    default_subtitle = preferred_default or next((stream for stream in normal_subtitles if is_default(stream)), None)
    default_subtitle = default_subtitle or (normal_subtitles[0] if normal_subtitles else None)

    for index, stream in enumerate(text_streams):
        suffix = subtitle_suffix(stream, stream is default_subtitle)
        target = destination / f"{base_name}{suffix}.srt"
        if target.exists():
            target = destination / f"{base_name}{suffix}.{index}.srt"

        language = stream_language(stream)
        title = stream_title(stream)
        print(f"  -> Extracting subtitle ({language}): {title} -> {target}")
        command = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            f"0:{stream['index']}",
            "-c:s",
            "srt",
            str(target),
        ]
        subprocess.run(command, check=False)


def copy_external_subtitles(source: Path, destination: Path, base_name: str) -> None:
    for subtitle in source.parent.glob(f"{base_name}*.srt"):
        if not is_allowed_subtitle_name(subtitle.name, base_name):
            continue
        target = destination / subtitle.name
        if not target.exists():
            shutil.copy2(subtitle, target)


def is_allowed_subtitle_name(name: str, base_name: str) -> bool:
    if not name.lower().startswith(base_name.lower()):
        return False
    relative_name = name[len(base_name):]
    if relative_name and relative_name[0] not in "._- ":
        return False
    lowered_name = relative_name.lower()
    if re.search(r"(?i)(^|[._-])(?:forced|forçada)(?=$|[._-])", lowered_name):
        return True
    language_tokens = re.findall(
        r"(?i)(?<![a-z])(?:pt(?:[-_][a-z]{2})?|por|portuguese|en(?:[-_][a-z]{2})?|eng|english)(?![a-z])",
        relative_name,
    )
    return bool(language_tokens)


def build_video_args(video: dict[str, Any], crf: int, source: Path, encoder: str = "libx264") -> list[str]:
    filter_prefix = ""
    if is_interlaced(video):
        print("  -> Video: interlaced source detected; applying deinterlacing...")
        filter_prefix = "yadif=mode=0:parity=auto:deint=all,"

    if is_hdr(video):
        print("  -> Video: HDR to SDR with tone mapping...")
        filter_value = (
            filter_prefix +
            "zscale=transfer=linear:npl=100,format=gbrpf32le,"
            "tonemap=mobius,zscale=transfer=bt709:matrix=bt709:primaries=bt709,"
            "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease,"
            "format=yuv420p"
        )
    else:
        print("  -> Video: H.264 SDR compatible...")
        range_filter = ",scale=in_range=full:out_range=limited" if is_full_range(video) else ""
        filter_value = (
            filter_prefix +
            "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease,"
            f"format=yuv420p{range_filter}"
        )

    common_tail = [
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "1",
        "-colorspace",
        "1",
        "-color_primaries",
        "1",
        "-color_trc",
        "1",
        "-fps_mode",
        "cfr",
    ]

    if encoder == "nvenc":
        print("  -> Video: using NVENC (GPU)...")
        args = [
            "-vf",
            filter_value,
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(crf),
            "-b:v",
            "0",
            *common_tail,
        ]
    else:
        args = [
            "-vf",
            filter_value,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-tune",
            "animation" if is_animation(source) else "film",
            "-crf",
            str(crf),
            *common_tail,
        ]
    output_frame_rate = target_frame_rate(video)
    if is_variable_frame_rate(video):
        print(f"  -> Video: VFR detected; normalizing to {output_frame_rate} fps...")
    if output_frame_rate:
        args.extend(["-r", output_frame_rate])
    return args


def select_audio_streams(audio_streams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(stream: dict[str, Any]) -> tuple[int, int, int, int]:
        original_index = int(stream.get("index", 0))
        return (
            1 if is_commentary(stream) else 0,
            0 if is_portuguese(stream) else 1,
            0 if is_default(stream) else 1,
            original_index,
        )

    return sorted(audio_streams, key=sort_key)


def build_audio_args(audio_streams: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    maps: list[str] = []
    codecs: list[str] = []
    metadata: list[str] = []
    selected_audio = select_audio_streams(audio_streams)
    if selected_audio:
        selected = selected_audio[0]
        print(f"  -> Default audio: {stream_language(selected)} - {stream_title(selected) or 'main track'}")

    for audio_index, stream in enumerate(selected_audio):
        maps.extend(["-map", f"0:{stream['index']}"])

        codec_name = str(stream.get("codec_name", "")).lower()
        channels = int(stream.get("channels", 0) or 0)
        sample_rate = str(stream.get("sample_rate", ""))
        audio_profile = str(stream.get("profile") or "").lower()

        if codec_name == "aac" and channels <= 2 and sample_rate == "48000" and audio_profile in DIRECT_PLAY_AUDIO_PROFILES:
            print(f"  -> Audio {audio_index}: already compatible, copying without re-encoding")
            codecs.extend(
                [
                    f"-c:a:{audio_index}",
                    "copy",
                    f"-disposition:a:{audio_index}",
                    "default" if audio_index == 0 else "0",
                ]
            )
        else:
            profile_note = ""
            if codec_name == "aac" and audio_profile not in DIRECT_PLAY_AUDIO_PROFILES:
                profile_note = f" (profile {stream.get('profile') or 'unknown'} is not accepted by the clients)"
            print(f"  -> Audio {audio_index}: converting to AAC stereo 48 kHz{profile_note}")
            codecs.extend(
                [
                    f"-c:a:{audio_index}",
                    "aac",
                    f"-profile:a:{audio_index}",
                    "aac_low",
                    f"-b:a:{audio_index}",
                    "160k",
                    f"-ac:a:{audio_index}",
                    "2",
                    f"-ar:a:{audio_index}",
                    "48000",
                    f"-disposition:a:{audio_index}",
                    "default" if audio_index == 0 else "0",
                ]
            )

        metadata.extend([f"-metadata:s:a:{audio_index}", f"language={normalized_language(stream)}"])
        metadata.extend([f"-metadata:s:a:{audio_index}", f"title={stream_title(stream)}"])
    return maps, codecs, metadata


NVENC_ERROR_HINTS = (
    (
        ("cuda_error_no_device", "no cuda-capable device", "no cuda capable device", "cuinit(0) failed"),
        "no CUDA-capable device detected (check that the GPU is visible to the process)",
    ),
    (("cannot load nvcuda", "nvcuda.dll", "cannot load libcuda", "libcuda"), "NVIDIA driver/CUDA not available"),
    (
        ("no nvenc capable devices", "no capable devices found", "openencodesessionex"),
        "no NVENC capable device available",
    ),
    (
        ("driver does not support", "minimum required nvidia driver"),
        "NVIDIA driver too old for this ffmpeg version",
    ),
    (
        ("unknown encoder", "encoder not found", "no such encoder"),
        "this ffmpeg build does not include the h264_nvenc encoder",
    ),
    (
        ("error setting option preset", "error setting option tune", "error setting option rc", "error setting option cq"),
        "NVENC options not supported by this ffmpeg version",
    ),
    (("error while opening encoder", "initialize encoder failed"), "failed to initialize the NVENC encoder"),
)


def detect_nvenc_error(video_log: Path) -> str | None:
    if not video_log.exists():
        return None
    text = video_log.read_text(encoding="utf-8", errors="replace")
    command_marker = text.rfind("Command:")
    if command_marker != -1:
        command_end = text.find("\n", command_marker)
        if command_end != -1:
            text = text[command_end + 1:]
    lowered = text.lower()
    for patterns, hint in NVENC_ERROR_HINTS:
        if any(pattern in lowered for pattern in patterns):
            return hint
    return None


def process_file(
    source: Path,
    root: Path,
    crf: int,
    root_log: Path,
    force_full: bool = False,
    analyze_only: bool = False,
    analysis_results: list[tuple[Path, str]] | None = None,
    decision_counts: dict[str, int] | None = None,
    overwrite: bool = False,
    encoder: str = "libx264",
) -> str:
    relative_parent = source.parent.relative_to(root)
    destination = root / "DirectPlay" / relative_parent
    log_directory = root / "Logs" / relative_parent
    log_directory.mkdir(parents=True, exist_ok=True)

    base_name = source.stem
    temporary = destination / f"{base_name}.tmp.mp4"
    output = destination / f"{base_name}.mp4"
    video_log = log_directory / f"{base_name}.log"
    progress_file = log_directory / f"{base_name}.progress"
    write_log(video_log, f"Starting: {source}")
    write_log(root_log, f"Starting: {source}")
    if output.exists() and not analyze_only and not overwrite:
        print(f"Skipping (already exists): {source}")
        write_log(video_log, "Skipped: output already exists")
        write_log(root_log, f"Skipped: output already exists - {source}")
        return "skipped"
    if temporary.exists():
        temporary.unlink()
    if progress_file.exists():
        progress_file.unlink()

    if not analyze_only:
        print(f"\n----------------------------------------\nProcessing: {source}")
    probe = run_probe(source)
    if probe is None:
        write_log(video_log, "Failure: ffprobe could not parse the input")
        write_log(root_log, f"ffprobe failure: {source}")
        return "failed"

    valid_input, input_problems = validate_input(source, probe)
    if not valid_input:
        print(f"Input rejected in validation: {source}", file=sys.stderr)
        for problem in input_problems:
            print(f"  - {problem}", file=sys.stderr)
            write_log(video_log, f"Input rejected: {problem}")
        write_log(root_log, f"Input rejected: {source}")
        return "rejected"

    video = first_video_stream(probe)
    if video is None:
        print(f"No video stream found: {source}", file=sys.stderr)
        return "failed"

    streams = probe.get("streams", [])
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]

    if force_full:
        mode = "full re-encode"
    elif is_remux_compatible(probe, video, audio):
        mode = "remux"
    elif is_video_copy_compatible(video):
        mode = "copy video + convert audio"
    else:
        mode = "full re-encode"

    if analyze_only and analysis_results is not None:
        analysis_results.append((source.relative_to(root), mode))
    else:
        print(f"  -> Decision: {mode}")
    if decision_counts is not None:
        decision_counts[mode] = decision_counts.get(mode, 0) + 1
    write_log(video_log, f"Decision: {mode}")
    write_log(root_log, f"Decision: {source} -> {mode}")
    if analyze_only:
        return "analyzed"

    destination.mkdir(parents=True, exist_ok=True)
    extract_subtitles(source, destination, base_name, subtitles)
    copy_external_subtitles(source, destination, base_name)

    if mode == "remux":
        print("  -> Video/audio already compatible; doing a fast REMUX...")
        write_log(video_log, "Mode: remux without re-encoding")
        audio_maps, _, metadata_args = build_audio_args(audio)
        disposition_args: list[str] = []
        for audio_index in range(len(audio)):
            disposition_args.extend([
                f"-disposition:a:{audio_index}",
                "default" if audio_index == 0 else "0",
            ])
        codec_args = ["-c", "copy", *disposition_args]
    elif mode == "copy video + convert audio":
        print("  -> Video compatible; copying video and converting audio only...")
        write_log(video_log, "Mode: video copy with audio conversion")
        audio_maps, audio_args, metadata_args = build_audio_args(audio)
        codec_args = ["-c:v", "copy"] + audio_args
    else:
        audio_maps, audio_args, metadata_args = build_audio_args(audio)
        codec_args = build_video_args(video, crf, source, encoder) + audio_args
        write_log(video_log, "Mode: full re-encode" + (" (nvenc)" if encoder == "nvenc" else ""))
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        f"0:{video['index']}",
        *audio_maps,
        *codec_args,
        *metadata_args,
        "-sn",
        "-map_metadata",
        "-1",
        "-metadata",
        "title=",
        "-movflags",
        "+faststart",
        "-progress",
        str(progress_file),
        "-stats_period",
        "1",
        str(temporary),
    ]

    write_log(video_log, "Command: " + " ".join(command))
    start_time = datetime.now()
    with video_log.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=log_handle,
        )
        last_progress = ""
        while process.poll() is None:
            if progress_file.exists():
                progress_data = {}
                for progress_line in progress_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "=" in progress_line:
                        key, value = progress_line.split("=", 1)
                        progress_data[key] = value
                out_time_ms = progress_data.get("out_time_ms", "")
                try:
                    out_seconds = float(out_time_ms) / 1_000_000
                except (TypeError, ValueError):
                    out_seconds = 0.0
                duration = parse_duration(probe.get("format", {}).get("duration"))
                percent = min(100.0, out_seconds / duration * 100) if duration else 0.0
                status = (
                    f"  -> {percent:6.2f}% | time {format_seconds(str(out_seconds))} | "
                    f"speed {progress_data.get('speed', '?')} | "
                    f"dup {progress_data.get('dup_frames', '0')} | "
                    f"drop {progress_data.get('drop_frames', '0')}"
                )
                if status != last_progress:
                    print(status, end="\r", flush=True)
                    last_progress = status
            time.sleep(1)
        result_code = process.wait()
    if progress_file.exists():
        progress_file.unlink()
    print()
    write_log(video_log, f"Conversion finished in {datetime.now() - start_time}")

    if result_code != 0 or not temporary.exists():
        temporary.unlink(missing_ok=True)
        print(f"Failed to convert: {source}", file=sys.stderr)
        write_log(video_log, f"Conversion failed: code {result_code}")
        if encoder == "nvenc":
            nvenc_error = detect_nvenc_error(video_log)
            detail = f": {nvenc_error}" if nvenc_error else ""
            print(f"  -> NVENC encoder failure{detail}.", file=sys.stderr)
            print("  -> No automatic fallback to CPU will be performed.", file=sys.stderr)
            print("  -> To process with CPU, run again with: --encoder libx264", file=sys.stderr)
            write_log(video_log, f"NVENC failed{detail}; no automatic fallback to CPU")
            write_log(root_log, f"NVENC failure (no CPU fallback): {source}")
            return "failed"
        write_log(root_log, f"Conversion failed: {source}")
        return "failed"

    if temporary.stat().st_size <= 1024 * 1024:
        temporary.unlink(missing_ok=True)
        print(f"Output discarded for being smaller than 1 MiB: {source}")
        write_log(video_log, "Output discarded: smaller than 1 MiB")
        write_log(root_log, f"Output discarded by size: {source}")
        return "rejected"

    valid, problems = validate_output(temporary)
    if not valid:
        temporary.unlink(missing_ok=True)
        print(f"Output rejected in validation: {source}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
            write_log(video_log, f"Output rejected: {problem}")
        write_log(root_log, f"Output rejected in validation: {source}")
        return "rejected"

    temporary.replace(output)
    print(f"Success -> {output}")
    write_log(video_log, f"Success: {output}")
    write_log(root_log, f"Success: {output}")
    return "success"


def is_anamorphic(video: dict[str, Any]) -> bool:
    sample_aspect_ratio = str(video.get("sample_aspect_ratio", "") or "").strip()
    return sample_aspect_ratio not in {"", "N/A", "0:1", "1:1"}


def audit_conditions(
    probe: dict[str, Any],
    video: dict[str, Any],
    audio_streams: list[dict[str, Any]],
    subtitle_streams: list[dict[str, Any]],
) -> list[str]:
    conditions: list[str] = []

    format_name = str(probe.get("format", {}).get("format_name", "")).lower()
    if "mp4" not in format_name:
        conditions.append("container")

    if str(video.get("codec_name", "")).lower() != "h264":
        conditions.append("video_codec")
    else:
        if str(video.get("profile", "")).lower() not in DIRECT_PLAY_VIDEO_PROFILES:
            conditions.append("video_profile")
        if int(video.get("level", 0) or 0) > 41:
            conditions.append("video_level")
    if str(video.get("pix_fmt", "")).lower() != "yuv420p":
        conditions.append("video_pix_fmt")
    if int(video.get("width", 0)) > 1920 or int(video.get("height", 0)) > 1080:
        conditions.append("video_resolution")
    if parse_frame_rate(video) > 30.001 or parse_frame_rate(video, "avg_frame_rate") > 30.001:
        conditions.append("video_fps")
    if is_variable_frame_rate(video):
        conditions.append("video_vfr")
    if is_interlaced(video):
        conditions.append("video_interlaced")
    if is_hdr(video):
        conditions.append("video_hdr")
    if is_anamorphic(video):
        conditions.append("video_anamorphic")

    for stream in audio_streams:
        if str(stream.get("codec_name", "")).lower() != "aac":
            conditions.append("audio_codec")
        if int(stream.get("channels", 0) or 0) > 2:
            conditions.append("audio_channels")
        if str(stream.get("sample_rate", "")) != "48000":
            conditions.append("audio_sample_rate")
        if str(stream.get("profile") or "").lower() not in DIRECT_PLAY_AUDIO_PROFILES:
            conditions.append("audio_profile")

    if any(str(stream.get("codec_name", "")).lower() in IMAGE_SUBTITLE_CODECS for stream in subtitle_streams):
        conditions.append("image_subtitles")

    unique: list[str] = []
    for condition in conditions:
        if condition not in unique:
            unique.append(condition)
    return unique


def audit_decision(probe: dict[str, Any], video: dict[str, Any], audio_streams: list[dict[str, Any]]) -> str:
    if is_remux_compatible(probe, video, audio_streams):
        return "nothing (already compatible)"
    if is_video_copy_compatible(video):
        return "copy video + convert audio"
    return "full re-encode"


def audit_library(sources: list[Path], root: Path, audit_log: Path, root_log: Path) -> int:
    decision_counts: dict[str, int] = {}
    condition_counts: dict[str, int] = {}
    condition_examples: dict[str, list[Path]] = {}
    unreadable: list[Path] = []

    print(f"Auditing {len(sources)} file(s). Nothing will be converted.\n")
    for index, source in enumerate(sources, start=1):
        probe = run_probe(source)
        video = first_video_stream(probe) if probe else None
        if probe is None or video is None:
            unreadable.append(source.relative_to(root))
            continue

        streams = probe.get("streams", [])
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]

        decision = audit_decision(probe, video, audio)
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

        relative = source.relative_to(root)
        for condition in audit_conditions(probe, video, audio, subtitles):
            condition_counts[condition] = condition_counts.get(condition, 0) + 1
            condition_examples.setdefault(condition, []).append(relative)

        if index % 100 == 0:
            print(f"  ... {index} file(s) audited")

    print(f"\nAudit finished: {len(sources)} file(s).\n")

    print("What the script would do:")
    for decision in ("nothing (already compatible)", "copy video + convert audio", "full re-encode"):
        count = decision_counts.get(decision, 0)
        if count:
            print(f"  {decision}: {count}")

    print("\nConditions found in the sources:")
    if not condition_counts:
        print("  none")
    for condition, label in AUDIT_CONDITION_LABELS.items():
        count = condition_counts.get(condition, 0)
        if not count:
            continue
        print(f"  {label}: {count}")
        for example in condition_examples.get(condition, [])[:AUDIT_MAX_EXAMPLES]:
            print(f"      - {example}")
        remaining = count - AUDIT_MAX_EXAMPLES
        if remaining > 0:
            print(f"      ... and {remaining} more file(s)")

    if unreadable:
        print(f"\nCould not be probed ({len(unreadable)}):")
        for relative in unreadable[:AUDIT_MAX_EXAMPLES]:
            print(f"  - {relative}")
        if len(unreadable) > AUDIT_MAX_EXAMPLES:
            print(f"  ... and {len(unreadable) - AUDIT_MAX_EXAMPLES} more file(s)")

    lines = [f"Direct Play audit - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", f"Root: {root}", ""]
    lines.append("== What the script would do ==")
    for decision, count in sorted(decision_counts.items()):
        lines.append(f"{decision}: {count}")
    lines.append("")
    lines.append("== Risk conditions (not mutually exclusive) ==")
    for condition, label in AUDIT_CONDITION_LABELS.items():
        count = condition_counts.get(condition, 0)
        if not count:
            continue
        lines.append(f"[{condition}] {label}: {count}")
        for example in condition_examples.get(condition, []):
            lines.append(f"  - {example}")
    if unreadable:
        lines.append("")
        lines.append("== Could not be probed ==")
        for relative in unreadable:
            lines.append(f"  - {relative}")
    audit_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nDetailed report: {audit_log}")
    write_log(root_log, f"Audit finished: {len(sources)} file(s)")
    for condition, count in sorted(condition_counts.items()):
        write_log(root_log, f"  {condition}: {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Converts videos to Jellyfin Direct Play MP4.")
    parser.add_argument("path", nargs="?", default=".", help="Root input directory")
    parser.add_argument("--crf", type=int, default=20, help="H.264 CRF, from 0 to 51 (default: 20)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--analyze", action="store_true", help="Only analyze and show the decision, without converting")
    mode_group.add_argument("--full", action="store_true", help="Force full re-encode of the video")
    mode_group.add_argument(
        "--audit",
        action="store_true",
        help="Only list the risk conditions of the sources, without converting (fast scan)",
    )
    parser.add_argument("--overwrite", action="store_true", help="Reprocess files that already have output")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reprocess only the files that failed in the previous run",
    )
    parser.add_argument(
        "--encoder",
        choices=["libx264", "nvenc"],
        default="libx264",
        help="Video encoder: libx264 (CPU, default) or nvenc (GPU)",
    )
    args = parser.parse_args()
    if not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")

    root = Path(args.path).resolve()
    if not root.is_dir():
        parser.error(f"Directory does not exist: {root}")

    output_root = root / "DirectPlay"
    root_log = root / "directplay_optimizer.log"
    write_log(root_log, "=" * 60)
    write_log(root_log, f"Processing start: {root}")
    if args.analyze:
        print("Analyze mode: no file will be converted.")
    elif args.audit:
        print("Audit mode: no file will be converted; only risk conditions will be listed.")
    elif args.full:
        print("Full re-encode mode enabled.")
    else:
        print("Hybrid mode enabled: remux, video copy or re-encode according to the analysis.")
    print("Starting MP4 Direct Play processing (Python)...")
    print(f"Search root folder: {root}")
    if not args.audit:
        print(f"Destination root folder: {output_root}")
    if args.encoder == "nvenc":
        print("Video encoder: nvenc (GPU)")
    print()

    failure_log = root / "directplay_failures.txt"
    legacy_failure_log = root / "directplay_falhas.txt"
    if args.retry_failed:
        if failure_log.exists():
            failure_source = failure_log
        elif legacy_failure_log.exists():
            failure_source = legacy_failure_log
        else:
            parser.error(f"No failure list found: {failure_log}")
        listed = [
            line.strip()
            for line in failure_source.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        existing = [root / entry for entry in listed if (root / entry).is_file()]
        if len(existing) != len(listed):
            print(f"{len(listed) - len(existing)} file(s) from the list no longer exist and will be ignored.")
        sources = existing
        print(f"Reprocessing {len(sources)} file(s) from the failure list.")
    else:
        sources = [
            source
            for source in root.rglob("*")
            if source.is_file()
            and source.suffix.lower() in VIDEO_EXTENSIONS
            and output_root not in source.parents
            and not source.name.lower().endswith((".tmp.mp4", ".tmp.mkv"))
        ]

    if args.audit:
        return audit_library(sources, root, root / "directplay_audit.txt", root_log)

    analysis_results: list[tuple[Path, str]] = []
    decision_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    failures: list[Path] = []
    for source in sources:
        outcome = process_file(
            source,
            root,
            args.crf,
            root_log,
            force_full=args.full,
            analyze_only=args.analyze,
            analysis_results=analysis_results,
            decision_counts=decision_counts,
            overwrite=args.overwrite,
            encoder=args.encoder,
        )
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        if outcome in RETRYABLE_OUTCOMES:
            failures.append(source.relative_to(root))

    if args.analyze:
        if len(analysis_results) == 1:
            relative_source, mode = analysis_results[0]
            source = root / relative_source
            print("\nFile analysis:")
            print(f"  file: {source}")
            print(f"  decision: {mode}")
        elif len(analysis_results) > 1:
            print("\nAnalysis summary by directory:")
            grouped: dict[Path, dict[str, int]] = {}
            for relative_source, mode in analysis_results:
                directory = relative_source.parent
                directory_modes = grouped.setdefault(directory, {})
                directory_modes[mode] = directory_modes.get(mode, 0) + 1
            for directory in sorted(grouped):
                label = "." if str(directory) == "." else str(directory)
                print(f"\n[{label}]")
                total = sum(grouped[directory].values())
                print(f"  files: {total}")
                for mode, count in sorted(grouped[directory].items()):
                    print(f"  {mode}: {count}")
        else:
            print("\nNo video found to analyze.")
    if decision_counts:
        write_log(root_log, "Decision summary:")
        for mode, count in sorted(decision_counts.items()):
            write_log(root_log, f"  {mode}: {count}")

    labels = {
        "success": "converted",
        "skipped": "skipped (output already existed)",
        "analyzed": "analyzed",
        "rejected": "rejected in validation",
        "failed": "failed",
    }
    print(f"\nSummary ({len(sources)} file(s)):")
    for outcome in labels:
        count = outcome_counts.get(outcome, 0)
        if count:
            print(f"  {labels[outcome]}: {count}")
    for outcome, count in sorted(outcome_counts.items()):
        if outcome not in labels:
            print(f"  {outcome}: {count}")
    write_log(root_log, "Outcome summary:")
    for outcome, count in sorted(outcome_counts.items()):
        write_log(root_log, f"  {outcome}: {count}")

    if failures:
        print(f"\nFailures ({len(failures)}):")
        for relative_source in failures[:20]:
            print(f"  - {relative_source}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more file(s)")
        failure_log.write_text("\n".join(str(path) for path in failures) + "\n", encoding="utf-8")
        print(f"List saved to: {failure_log}")
        print("To retry: --retry-failed")
        write_log(root_log, f"Failures written to {failure_log}")
    elif not args.analyze:
        failure_log.unlink(missing_ok=True)
        legacy_failure_log.unlink(missing_ok=True)

    write_log(root_log, "Processing finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())