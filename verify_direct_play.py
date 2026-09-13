from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime


PLAY_METHOD_LABEL = {
    "DirectPlay": "DIRECT PLAY",
    "DirectStream": "DIRECT STREAM (server-side remux)",
    "Transcode": "TRANSCODE  <-- will not play when transcoding is disabled",
}

# TranscodeReasons most relevant to the optimizer target.
REASON_HINTS = {
    "ContainerNotSupported": "unsupported container (a remux would solve it)",
    "VideoCodecNotSupported": "unsupported video codec",
    "VideoProfileNotSupported": "video profile outside what the client accepts",
    "VideoLevelNotSupported": "video level above what the client accepts",
    "VideoBitrateNotSupported": "video bitrate above the client limit",
    "VideoFramerateNotSupported": "frame rate above the client limit",
    "VideoResolutionNotSupported": "resolution above the client limit",
    "VideoRangeTypeNotSupported": "HDR/Dolby Vision not supported by the client",
    "AnamorphicVideoNotSupported": "anamorphic video not supported by the client",
    "RefFramesNotSupported": "reference frames above the client limit",
    "InterlacedVideoNotSupported": "interlaced video",
    "AudioCodecNotSupported": "unsupported audio codec",
    "AudioProfileNotSupported": "unsupported audio profile (e.g. HE-AAC)",
    "AudioChannelsNotSupported": "audio channels above the client limit",
    "AudioBitrateNotSupported": "audio bitrate above the client limit",
    "AudioIsExternal": "external audio track",
    "SecondaryAudioNotSupported": "secondary audio track not supported",
    "SubtitleCodecNotSupported": "unsupported subtitle format",
}


class ApiError(Exception):
    pass


def http_get(url: str, api_key: str | None = None, timeout: int = 15) -> object:
    request = urllib.request.Request(url, method="GET")
    if api_key:
        request.add_header("X-Emby-Token", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise ApiError(
                "Key rejected by the server (401/403). Check that JELLYFIN_API_KEY "
                "is set to a valid, active key."
            ) from error
        raise ApiError(f"HTTP {error.code} on {url}") from error
    except urllib.error.URLError as error:
        raise ApiError(f"Connection failure to {url}: {error}") from error
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise ApiError(f"Response was not JSON on {url}") from error


def fetch_public_info(base_url: str) -> dict:
    return http_get(f"{base_url}/System/Info/Public")  # type: ignore[return-value]


def fetch_sessions(base_url: str, api_key: str) -> list[dict]:
    data = http_get(f"{base_url}/Sessions", api_key)
    return data if isinstance(data, list) else []


def format_position(ticks: object) -> str:
    try:
        total = int(ticks) // 10_000_000  # noqa: ERA001 - ticks are 100ns
    except (TypeError, ValueError):
        return "--:--:--"
    hours, remainder = divmod(max(0, total), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def describe_session(session: dict) -> str | None:
    item = session.get("NowPlayingItem")
    if not isinstance(item, dict):
        return None

    method = str(session.get("PlayState", {}).get("PlayMethod", "") or "?")
    label = PLAY_METHOD_LABEL.get(method, method)
    client = str(session.get("Client", "?"))
    device = str(session.get("DeviceName", "?"))
    user = str(session.get("UserName", "?"))
    title = str(item.get("Name", "?"))

    media_sources = item.get("MediaSources") or []
    source = media_sources[0] if media_sources else {}
    path = str(source.get("Path", "") or "")
    video = str(source.get("VideoType", "") or "")
    container = str(source.get("Container", "") or "")

    lines = [
        f"{label}",
        f"    client  : {client} ({device})  user: {user}",
        f"    item    : {title}",
    ]
    if path:
        lines.append(f"    file    : {path}")
    if container or video:
        lines.append(f"    source  : container={container or '?'} video={video or '?'}")
    if "SupportsDirectPlay" in source:
        lines.append(
            f"    server  : SupportsDirectPlay={source.get('SupportsDirectPlay')} "
            f"SupportsDirectStream={source.get('SupportsDirectStream')}"
        )
    position = format_position(session.get("PlayState", {}).get("PositionTicks"))
    lines.append(f"    position: {position}")

    transcoding = session.get("TranscodingInfo")
    if isinstance(transcoding, dict):
        reasons = transcoding.get("TranscodeReasons") or []
        if reasons:
            lines.append("    REASONS:")
            for reason in reasons:
                hint = REASON_HINTS.get(str(reason), "")
                lines.append(f"      - {reason}" + (f"  ({hint})" if hint else ""))
        lines.append(
            "    transcode: "
            f"video={'direct' if transcoding.get('IsVideoDirect') else 're-encoded'} "
            f"audio={'direct' if transcoding.get('IsAudioDirect') else 're-encoded'} "
            f"v={transcoding.get('VideoCodec')} a={transcoding.get('AudioCodec')}"
        )
    return "\n".join(lines)


def session_key(session: dict) -> str:
    return str(session.get("Id", "")) + "|" + str(session.get("DeviceId", ""))


def print_report(sessions: list[dict], raw: bool) -> None:
    if raw:
        print(json.dumps(sessions, indent=2, ensure_ascii=False))
        return

    playing = [session for session in sessions if isinstance(session.get("NowPlayingItem"), dict)]
    if not playing:
        print("No session is playing right now. Start playback on a client and run again.")
        return

    for session in playing:
        report = describe_session(session)
        if report:
            print(report)
            print("-" * 60)


def watch(base_url: str, api_key: str, interval: float) -> int:
    last_state: dict[str, str] = {}
    print("Watching sessions. Start playback on the clients. (Ctrl+C to exit)\n")
    while True:
        try:
            sessions = fetch_sessions(base_url, api_key)
        except ApiError as error:
            print(f"error: {error}", file=sys.stderr)
            time.sleep(max(5.0, interval))
            continue

        current_keys: set[str] = set()
        for session in sessions:
            key = session_key(session)
            report = describe_session(session)
            current_keys.add(key)
            if report is None:
                continue
            if last_state.get(key) != report:
                last_state[key] = report
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{stamp}]\n{report}\n" + "-" * 60)

        for key in list(last_state):
            if key not in current_keys:
                del last_state[key]
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{stamp}] session ended (active clients: {len(current_keys)})\n" + "-" * 60)

        time.sleep(max(1.0, interval))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shows, for each active Jellyfin session, whether playback is Direct Play, Direct Stream or Transcode."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("JELLYFIN_URL", "http://localhost:8096"),
        help="Jellyfin server base URL (default: JELLYFIN_URL env var or http://localhost:8096)",
    )
    parser.add_argument("--once", action="store_true", help="Read once and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="Delay between reads in continuous mode")
    parser.add_argument("--json", action="store_true", help="Print the raw session JSON (debugging)")
    args = parser.parse_args()

    api_key = os.environ.get("JELLYFIN_API_KEY", "").strip()
    if not api_key:
        print("The JELLYFIN_API_KEY environment variable is not set.", file=sys.stderr)
        print('PowerShell: $env:JELLYFIN_API_KEY = (Read-Host "API key" -AsSecureString | ConvertFrom-SecureString -AsPlainText)', file=sys.stderr)
        return 2

    base_url = args.url.rstrip("/")
    try:
        info = fetch_public_info(base_url)
    except ApiError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Server: {info.get('ServerName', '?')} | Jellyfin {info.get('Version', '?')}")
    print(f"URL   : {base_url}\n")

    if args.once:
        try:
            sessions = fetch_sessions(base_url, api_key)
        except ApiError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print_report(sessions, args.json)
        return 0

    try:
        return watch(base_url, api_key, args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
