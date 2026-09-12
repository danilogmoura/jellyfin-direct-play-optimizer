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
        print(f"Falha ao analisar: {source}", file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        print(f"JSON invalido do ffprobe para {source}: {error}", file=sys.stderr)
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
        problems.append("duracao ausente ou invalida")

    video = first_video_stream(probe)
    if video is None:
        problems.append("nenhum fluxo de video valido")
    else:
        video_duration = parse_duration(video.get("duration"))
        if video_duration > 0 and format_duration > 0:
            difference = abs(format_duration - video_duration)
            if difference > max(2.0, format_duration * 0.05):
                problems.append("duracao do video difere significativamente do container")

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
            problems.append(f"falha ao ler o final do fluxo de {label}")
            if result.stderr.strip():
                print(f"  -> Diagnostico: {result.stderr.strip()}", file=sys.stderr)
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
        return False, ["ffprobe nao conseguiu ler a saida"]

    problems: list[str] = []
    format_name = str(probe.get("format", {}).get("format_name", "")).lower()
    if "mp4" not in format_name:
        problems.append(f"container invalido: {format_name or 'desconhecido'}")

    videos = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    if len(videos) != 1:
        problems.append(f"quantidade de videos invalida: {len(videos)}")
    else:
        video = videos[0]
        if str(video.get("codec_name", "")).lower() != "h264":
            problems.append(f"codec de video invalido: {video.get('codec_name', 'desconhecido')}")
        if str(video.get("pix_fmt", "")).lower() != "yuv420p":
            problems.append(f"pixel format invalido: {video.get('pix_fmt', 'desconhecido')}")
        if int(video.get("width", 0)) > 1920 or int(video.get("height", 0)) > 1080:
            problems.append(f"resolucao acima do limite: {video.get('width')}x{video.get('height')}")
        if parse_frame_rate(video) > 30.001 or parse_frame_rate(video, "avg_frame_rate") > 30.001:
            problems.append("framerate acima de 30 fps")
        if is_hdr(video):
            problems.append("saida ainda contem metadados HDR")
        if str(video.get("field_order", "progressive")).lower() not in {"progressive", "unknown", ""}:
            problems.append("saida ainda esta entrelacada")

    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        if str(stream.get("codec_name", "")).lower() != "aac":
            problems.append(f"codec de audio invalido: {stream.get('codec_name', 'desconhecido')}")
        if int(stream.get("channels", 0)) > 2:
            problems.append("audio com mais de dois canais")
        if str(stream.get("sample_rate", "")) != "48000":
            problems.append(f"sample rate de audio invalido: {stream.get('sample_rate', 'desconhecido')}")

    return not problems, problems


def is_remux_compatible(probe: dict[str, Any], video: dict[str, Any], audio_streams: list[dict[str, Any]]) -> bool:
    format_name = str(probe.get("format", {}).get("format_name", "")).lower()
    if "mp4" not in format_name or not is_video_copy_compatible(video):
        return False
    return all(
        str(stream.get("codec_name", "")).lower() == "aac"
        and int(stream.get("channels", 0)) <= 2
        and str(stream.get("sample_rate", "")) == "48000"
        for stream in audio_streams
    )


def is_video_copy_compatible(video: dict[str, Any]) -> bool:
    profile = str(video.get("profile", "")).lower()
    level = int(video.get("level", 0) or 0)
    return (
        str(video.get("codec_name", "")).lower() == "h264"
        and profile in {"baseline", "main", "high"}
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
        print(f"  -> Extraindo legenda ({language}): {title} -> {target}")
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
    relative_name = name[len(base_name):] if name.lower().startswith(base_name.lower()) else name
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
        print("  -> Video: Entrelaçado detectado; aplicando desentrelaçamento...")
        filter_prefix = "yadif=mode=0:parity=auto:deint=all,"

    if is_hdr(video):
        print("  -> Video: HDR para SDR com tonemapping...")
        filter_value = (
            filter_prefix +
            "zscale=transfer=linear:npl=100,format=gbrpf32le,"
            "tonemap=mobius,zscale=transfer=bt709:matrix=bt709:primaries=bt709,"
            "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease,"
            "format=yuv420p"
        )
    else:
        print("  -> Video: H.264 SDR compativel...")
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
        print("  -> Video: usando NVENC (GPU)...")
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
        print(f"  -> Video: VFR detectado; normalizando para {output_frame_rate} fps...")
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
        print(f"  -> Audio padrão: {stream_language(selected)} - {stream_title(selected) or 'faixa principal'}")

    for audio_index, stream in enumerate(selected_audio):
        maps.extend(["-map", f"0:{stream['index']}"])

        codec_name = str(stream.get("codec_name", "")).lower()
        channels = int(stream.get("channels", 0) or 0)
        sample_rate = str(stream.get("sample_rate", ""))

        if codec_name == "aac" and channels <= 2 and sample_rate == "48000":
            print(f"  -> Audio {audio_index}: ja compativel, copiando sem recodificar")
            codecs.extend(
                [
                    f"-c:a:{audio_index}",
                    "copy",
                    f"-disposition:a:{audio_index}",
                    "default" if audio_index == 0 else "0",
                ]
            )
        else:
            print(f"  -> Audio {audio_index}: convertendo para AAC estereo 48 kHz")
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
    (("cannot load nvcuda", "nvcuda.dll", "cannot load libcuda", "libcuda"), "driver/CUDA da NVIDIA nao disponivel"),
    (
        ("no nvenc capable devices", "no capable devices found", "openencodesessionex"),
        "nenhum dispositivo NVENC disponivel",
    ),
    (("unknown encoder", "h264_nvenc"), "o ffmpeg atual nao inclui o encoder h264_nvenc"),
    (
        ("driver does not support", "minimum required nvidia driver"),
        "driver NVIDIA antigo para esta versao do ffmpeg",
    ),
    (
        ("error setting option preset", "error setting option tune", "error setting option rc", "error setting option cq"),
        "opcoes do NVENC nao suportadas por esta versao do ffmpeg",
    ),
    (("error while opening encoder", "initialize encoder failed"), "falha ao inicializar o encoder NVENC"),
)


def detect_nvenc_error(video_log: Path) -> str | None:
    if not video_log.exists():
        return None
    text = video_log.read_text(encoding="utf-8", errors="replace")
    command_marker = text.rfind("Comando:")
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
) -> None:
    relative_parent = source.parent.relative_to(root)
    destination = root / "DirectPlay" / relative_parent
    log_directory = root / "Logs" / relative_parent
    log_directory.mkdir(parents=True, exist_ok=True)

    base_name = source.stem
    temporary = destination / f"{base_name}.tmp.mp4"
    output = destination / f"{base_name}.mp4"
    video_log = log_directory / f"{base_name}.log"
    progress_file = log_directory / f"{base_name}.progress"
    write_log(video_log, f"Iniciando: {source}")
    write_log(root_log, f"Iniciando: {source}")
    if output.exists() and not analyze_only and not overwrite:
        print(f"Pulando (ja existe): {source}")
        write_log(video_log, "Ignorado: saida ja existe")
        write_log(root_log, f"Ignorado: saida ja existe - {source}")
        return
    if temporary.exists():
        temporary.unlink()
    if progress_file.exists():
        progress_file.unlink()

    if not analyze_only:
        print(f"\n----------------------------------------\nProcessando: {source}")
    probe = run_probe(source)
    if probe is None:
        write_log(video_log, "Falha: ffprobe nao conseguiu analisar a entrada")
        write_log(root_log, f"Falha no ffprobe: {source}")
        return

    valid_input, input_problems = validate_input(source, probe)
    if not valid_input:
        print(f"Entrada rejeitada na validacao: {source}", file=sys.stderr)
        for problem in input_problems:
            print(f"  - {problem}", file=sys.stderr)
            write_log(video_log, f"Entrada rejeitada: {problem}")
        write_log(root_log, f"Entrada rejeitada: {source}")
        return

    video = first_video_stream(probe)
    if video is None:
        print(f"Nenhum fluxo de video encontrado: {source}", file=sys.stderr)
        return

    streams = probe.get("streams", [])
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]

    if force_full:
        mode = "recodificacao completa"
    elif is_remux_compatible(probe, video, audio):
        mode = "remux"
    elif is_video_copy_compatible(video):
        mode = "copiar video + converter audio"
    else:
        mode = "recodificacao completa"

    if analyze_only and analysis_results is not None:
        analysis_results.append((source.relative_to(root), mode))
    else:
        print(f"  -> Decisao: {mode}")
    if decision_counts is not None:
        decision_counts[mode] = decision_counts.get(mode, 0) + 1
    write_log(video_log, f"Decisao: {mode}")
    write_log(root_log, f"Decisao: {source} -> {mode}")
    if analyze_only:
        return

    destination.mkdir(parents=True, exist_ok=True)
    extract_subtitles(source, destination, base_name, subtitles)
    copy_external_subtitles(source, destination, base_name)

    if mode == "remux":
        print("  -> Video/audio ja compativeis; fazendo REMUX rapido...")
        write_log(video_log, "Modo: remux sem recodificacao")
        audio_maps, _, metadata_args = build_audio_args(audio)
        disposition_args: list[str] = []
        for audio_index in range(len(audio)):
            disposition_args.extend([
                f"-disposition:a:{audio_index}",
                "default" if audio_index == 0 else "0",
            ])
        codec_args = ["-c", "copy", *disposition_args]
    elif mode == "copiar video + converter audio":
        print("  -> Video compativel; copiando video e convertendo apenas o audio...")
        write_log(video_log, "Modo: video copy com conversao de audio")
        audio_maps, audio_args, metadata_args = build_audio_args(audio)
        codec_args = ["-c:v", "copy"] + audio_args
    else:
        audio_maps, audio_args, metadata_args = build_audio_args(audio)
        codec_args = build_video_args(video, crf, source, encoder) + audio_args
        write_log(video_log, "Modo: recodificacao" + (" (nvenc)" if encoder == "nvenc" else ""))
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
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

    write_log(video_log, "Comando: " + " ".join(command))
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
                    f"  -> {percent:6.2f}% | tempo {format_seconds(str(out_seconds))} | "
                    f"velocidade {progress_data.get('speed', '?')} | "
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
    write_log(video_log, f"Conversao finalizada em {datetime.now() - start_time}")

    if result_code != 0 or not temporary.exists():
        temporary.unlink(missing_ok=True)
        print(f"Falha ao converter: {source}", file=sys.stderr)
        write_log(video_log, f"Falha na conversao: codigo {result_code}")
        if encoder == "nvenc":
            nvenc_error = detect_nvenc_error(video_log)
            detail = f": {nvenc_error}" if nvenc_error else ""
            print(f"  -> Falha no encoder NVENC{detail}.", file=sys.stderr)
            print("  -> Nenhum fallback automatico para CPU sera feito.", file=sys.stderr)
            print("  -> Para processar com CPU, rode novamente com: --encoder libx264", file=sys.stderr)
            write_log(video_log, f"NVENC falhou{detail}; sem fallback automatico para CPU")
            write_log(root_log, f"Falha no NVENC (sem fallback para CPU): {source}")
            return
        write_log(root_log, f"Falha na conversao: {source}")
        return

    if temporary.stat().st_size <= 1024 * 1024:
        temporary.unlink(missing_ok=True)
        print(f"Saida descartada por ser menor que 1 MiB: {source}")
        write_log(video_log, "Saida descartada: menor que 1 MiB")
        write_log(root_log, f"Saida descartada por tamanho: {source}")
        return

    valid, problems = validate_output(temporary)
    if not valid:
        temporary.unlink(missing_ok=True)
        print(f"Saida rejeitada na validacao: {source}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
            write_log(video_log, f"Saida rejeitada: {problem}")
        write_log(root_log, f"Saida rejeitada na validacao: {source}")
        return

    temporary.replace(output)
    print(f"Sucesso -> {output}")
    write_log(video_log, f"Sucesso: {output}")
    write_log(root_log, f"Sucesso: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Converte videos para MP4 Direct Play do Jellyfin.")
    parser.add_argument("path", nargs="?", default=".", help="Diretorio raiz de entrada")
    parser.add_argument("--crf", type=int, default=20, help="CRF do H.264, de 0 a 51 (padrao: 20)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--analyze", action="store_true", help="Apenas analisa e mostra a decisao, sem converter")
    mode_group.add_argument("--full", action="store_true", help="Forca recodificacao completa do video")
    parser.add_argument("--overwrite", action="store_true", help="Reprocessa arquivos que ja possuem saida")
    parser.add_argument(
        "--encoder",
        choices=["libx264", "nvenc"],
        default="libx264",
        help="Encoder de video: libx264 (CPU, padrao) ou nvenc (GPU)",
    )
    args = parser.parse_args()
    if not 0 <= args.crf <= 51:
        parser.error("--crf deve estar entre 0 e 51")

    root = Path(args.path).resolve()
    if not root.is_dir():
        parser.error(f"Diretorio inexistente: {root}")

    output_root = root / "DirectPlay"
    root_log = root / "directplay_optimizer.log"
    write_log(root_log, "=" * 60)
    write_log(root_log, f"Inicio do processamento: {root}")
    if args.analyze:
        print("Modo analise: nenhum arquivo sera convertido.")
    elif args.full:
        print("Modo recodificacao completa ativado.")
    else:
        print("Modo hibrido ativado: remux, video copy ou recodificacao conforme a analise.")
    print("Iniciando processamento MP4 Direct Play (Python)...")
    print(f"Pasta Raiz de Busca: {root}")
    print(f"Pasta Raiz de Destino: {output_root}")
    if args.encoder == "nvenc":
        print("Encoder de video: nvenc (GPU)")
    print()

    analysis_results: list[tuple[Path, str]] = []
    decision_counts: dict[str, int] = {}
    for source in root.rglob("*"):
        if not source.is_file() or source.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if output_root in source.parents or source.name.lower().endswith((".tmp.mp4", ".tmp.mkv")):
            continue
        process_file(
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

    if args.analyze:
        if len(analysis_results) == 1:
            relative_source, mode = analysis_results[0]
            source = root / relative_source
            print("\nAnalise do arquivo:")
            print(f"  arquivo: {source}")
            print(f"  decisao: {mode}")
        elif len(analysis_results) > 1:
            print("\nResumo da analise por diretorio:")
            grouped: dict[Path, dict[str, int]] = {}
            for relative_source, mode in analysis_results:
                directory = relative_source.parent
                directory_modes = grouped.setdefault(directory, {})
                directory_modes[mode] = directory_modes.get(mode, 0) + 1
            for directory in sorted(grouped):
                label = "." if str(directory) == "." else str(directory)
                print(f"\n[{label}]")
                total = sum(grouped[directory].values())
                print(f"  arquivos: {total}")
                for mode, count in sorted(grouped[directory].items()):
                    print(f"  {mode}: {count}")
        else:
            print("\nNenhum video encontrado para analisar.")
    if decision_counts:
        write_log(root_log, "Resumo das decisoes:")
        for mode, count in sorted(decision_counts.items()):
            write_log(root_log, f"  {mode}: {count}")
    write_log(root_log, "Processamento concluido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())