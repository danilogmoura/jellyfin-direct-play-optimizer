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
    "DirectStream": "DIRECT STREAM (remux feito pelo servidor)",
    "Transcode": "TRANSCODE  <-- nao toca no seu servidor (transcode desabilitado)",
}

# TranscodeReasons mais relevantes para o alvo do otimizador.
REASON_HINTS = {
    "ContainerNotSupported": "container incompativel (o remux resolveria)",
    "VideoCodecNotSupported": "codec de video incompativel",
    "VideoProfileNotSupported": "profile de video fora do aceito pelo cliente",
    "VideoLevelNotSupported": "level de video acima do aceito pelo cliente",
    "VideoBitrateNotSupported": "bitrate de video acima do limite do cliente",
    "VideoFramerateNotSupported": "framerate acima do limite do cliente",
    "VideoResolutionNotSupported": "resolucao acima do limite do cliente",
    "VideoRangeTypeNotSupported": "HDR/Dolby Vision sem suporte no cliente",
    "AnamorphicVideoNotSupported": "video anamorfico sem suporte no cliente",
    "RefFramesNotSupported": "ref frames acima do limite do cliente",
    "InterlacedVideoNotSupported": "video entrelacado",
    "AudioCodecNotSupported": "codec de audio incompativel",
    "AudioProfileNotSupported": "profile de audio (ex.: HE-AAC) nao suportado",
    "AudioChannelsNotSupported": "canais de audio acima do limite do cliente",
    "AudioBitrateNotSupported": "bitrate de audio acima do limite do cliente",
    "AudioIsExternal": "faixa de audio externa",
    "SecondaryAudioNotSupported": "faixa de audio secundaria nao suportada",
    "SubtitleCodecNotSupported": "formato de legenda nao suportado",
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
                "Chave recusada pelo servidor (401/403). Confira se JELLYFIN_API_KEY "
                "foi definida com uma chave valida e ativa."
            ) from error
        raise ApiError(f"HTTP {error.code} em {url}") from error
    except urllib.error.URLError as error:
        raise ApiError(f"Falha de conexao com {url}: {error}") from error
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise ApiError(f"Resposta nao era JSON em {url}") from error


def fetch_public_info(base_url: str) -> dict:
    return http_get(f"{base_url}/System/Info/Public")  # type: ignore[return-value]


def fetch_sessions(base_url: str, api_key: str) -> list[dict]:
    data = http_get(f"{base_url}/Sessions", api_key)
    return data if isinstance(data, list) else []


def format_position(ticks: object) -> str:
    try:
        total = int(ticks) // 10_000_000  # noqa: ERA001 - ticks sao 100ns
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
        f"    cliente : {client} ({device})  usuario: {user}",
        f"    item    : {title}",
    ]
    if path:
        lines.append(f"    arquivo : {path}")
    if container or video:
        lines.append(f"    origem  : container={container or '?'} video={video or '?'}")
    if "SupportsDirectPlay" in source:
        lines.append(
            f"    servidor: SupportsDirectPlay={source.get('SupportsDirectPlay')} "
            f"SupportsDirectStream={source.get('SupportsDirectStream')}"
        )
    position = format_position(session.get("PlayState", {}).get("PositionTicks"))
    lines.append(f"    posicao : {position}")

    transcoding = session.get("TranscodingInfo")
    if isinstance(transcoding, dict):
        reasons = transcoding.get("TranscodeReasons") or []
        if reasons:
            lines.append("    MOTIVOS:")
            for reason in reasons:
                hint = REASON_HINTS.get(str(reason), "")
                lines.append(f"      - {reason}" + (f"  ({hint})" if hint else ""))
        lines.append(
            "    transcode: "
            f"video={'direto' if transcoding.get('IsVideoDirect') else 'reencodado'} "
            f"audio={'direto' if transcoding.get('IsAudioDirect') else 'reencodado'} "
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
        print("Nenhuma sessao reproduzindo agora. Inicie o playback em um cliente e rode de novo.")
        return

    for session in playing:
        report = describe_session(session)
        if report:
            print(report)
            print("-" * 60)


def watch(base_url: str, api_key: str, interval: float) -> int:
    last_state: dict[str, str] = {}
    print("Monitorando sessoes. Inicie o playback nos clientes. (Ctrl+C para sair)\n")
    while True:
        try:
            sessions = fetch_sessions(base_url, api_key)
        except ApiError as error:
            print(f"erro: {error}", file=sys.stderr)
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
                print(f"[{stamp}] sessao encerrada (clientes ativos: {len(current_keys)})\n" + "-" * 60)

        time.sleep(max(1.0, interval))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mostra, por sessao ativa no Jellyfin, se a reproducao e Direct Play, Direct Stream ou Transcode."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("JELLYFIN_URL", "http://localhost:8096"),
        help="URL base do servidor Jellyfin (padrao: variavel JELLYFIN_URL ou http://localhost:8096)",
    )
    parser.add_argument("--once", action="store_true", help="Faz uma unica leitura e sai")
    parser.add_argument("--interval", type=float, default=5.0, help="Intervalo entre leituras no modo continuo")
    parser.add_argument("--json", action="store_true", help="Imprime o JSON bruto das sessoes (depuracao)")
    args = parser.parse_args()

    api_key = os.environ.get("JELLYFIN_API_KEY", "").strip()
    if not api_key:
        print("A variavel de ambiente JELLYFIN_API_KEY nao esta definida.", file=sys.stderr)
        print('PowerShell: $env:JELLYFIN_API_KEY = (Read-Host "API key" -AsSecureString | ConvertFrom-SecureString -AsPlainText)', file=sys.stderr)
        return 2

    base_url = args.url.rstrip("/")
    try:
        info = fetch_public_info(base_url)
    except ApiError as error:
        print(f"erro: {error}", file=sys.stderr)
        return 1

    print(f"Servidor: {info.get('ServerName', '?')} | Jellyfin {info.get('Version', '?')}")
    print(f"URL     : {base_url}\n")

    if args.once:
        try:
            sessions = fetch_sessions(base_url, api_key)
        except ApiError as error:
            print(f"erro: {error}", file=sys.stderr)
            return 1
        print_report(sessions, args.json)
        return 0

    try:
        return watch(base_url, api_key, args.interval)
    except KeyboardInterrupt:
        print("\nEncerrado.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
