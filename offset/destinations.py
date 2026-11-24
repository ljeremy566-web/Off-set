"""Destination presets (Twitch, YouTube, Kick, Local, Custom) and safe URL masking."""
from urllib.parse import urlsplit

PRESETS = {
    "local": {
        "id": "local",
        "name": "Local (FFmpeg / MediaMTX)",
        "base_url": "rtmp://127.0.0.1:1936/live/",
        "default_key": "offset",
        "needs_key": False,
        "placeholder": "rtmp://127.0.0.1:1936/live/offset",
        "help": "Pruebas locales en tu PC sin salir a internet.",
    },
    "twitch": {
        "id": "twitch",
        "name": "Twitch",
        "base_url": "rtmps://ingest.global-contribute.live-video.net/app/",
        "alt_url": "rtmp://live.twitch.tv/app/",
        "default_key": "",
        "needs_key": True,
        "placeholder": "live_123456789_xxxxxxxxxx",
        "help": "Pega tu clave de emisión principal de Twitch (Panel → Ajustes → Emisión).",
    },
    "youtube": {
        "id": "youtube",
        "name": "YouTube Live",
        "base_url": "rtmps://a.rtmps.youtube.com/live2/",
        "alt_url": "rtmp://a.rtmp.youtube.com/live2/",
        "default_key": "",
        "needs_key": True,
        "placeholder": "xxxx-xxxx-xxxx-xxxx-xxxx",
        "help": "Pega tu clave de emisión de YouTube Studio.",
    },
    "kick": {
        "id": "kick",
        "name": "Kick",
        "base_url": "rtmps://fa723fc1b171.global-contribute.live-video.net/app/",
        "default_key": "",
        "needs_key": True,
        "placeholder": "sk_us-west-2_xxxxxxxxxx",
        "help": "Pega tu Stream Key de Kick (Creator Dashboard → Settings → Stream Key).",
    },
    "custom": {
        "id": "custom",
        "name": "RTMP / RTMPS personalizado",
        "base_url": "",
        "default_key": "",
        "needs_key": False,
        "placeholder": "rtmps://servidor.ejemplo/app/tu_clave",
        "help": "Introduce la URL completa rtmp:// o rtmps:// incluyendo tu clave.",
    },
}


def local_rtmp(value):
    """Strict loopback-only RTMP validation (used for OBS local input and strict mode)."""
    try:
        url = urlsplit(value)
        if (url.scheme != "rtmp" or url.hostname != "127.0.0.1"
                or url.username or url.password or url.query or url.fragment
                or not url.port or not 1024 <= url.port <= 65535
                or len(url.path.strip("/").split("/")) != 2
                or any(c.isspace() for c in value)):
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError("Usa rtmp://127.0.0.1:PUERTO/aplicacion/clave; solo pruebas locales.") from None
    return value


def validate_output_rtmp(value, *, allow_remote=True):
    """Validate an RTMP or RTMPS output destination safely."""
    if not allow_remote:
        return local_rtmp(value)
    try:
        if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError()
        url = urlsplit(value)
        if url.scheme not in ("rtmp", "rtmps"):
            raise ValueError()
        if not url.hostname or url.username or url.password or url.fragment:
            raise ValueError()
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError()
        parts = [p for p in url.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError()
        if url.hostname in ("127.0.0.1", "localhost"):
            if not url.port or not 1024 <= url.port <= 65535:
                raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError("Destino inválido. Usa una URL rtmp:// o rtmps:// válida con aplicación y clave.") from None
    return value


def build_output_url(preset_id, stream_key="", custom_url=""):
    """Build and validate a destination URL from a preset + key or custom URL."""
    preset_id = str(preset_id or "local").strip().lower()
    if preset_id not in PRESETS:
        raise ValueError("Destino desconocido.")
    if preset_id == "custom":
        return validate_output_rtmp(str(custom_url or "").strip(), allow_remote=True)
    if preset_id == "local":
        raw = str(custom_url or "").strip()
        if raw:
            return validate_output_rtmp(raw, allow_remote=True)
        key = str(stream_key or PRESETS["local"]["default_key"]).strip().lstrip("/")
        return validate_output_rtmp(PRESETS["local"]["base_url"] + key, allow_remote=True)
    preset = PRESETS[preset_id]
    key = str(stream_key or "").strip().lstrip("/")
    if not key:
        raise ValueError(f"Introduce tu Stream Key de {preset['name']}.")
    base = str(custom_url or "").strip()
    if base and (base.startswith("rtmp://") or base.startswith("rtmps://")):
        url = base.rstrip("/") + "/" + key if not base.endswith(key) else base
    else:
        url = preset["base_url"] + key
    return validate_output_rtmp(url, allow_remote=True)


def mask_rtmp_url(value):
    """Mask stream keys in URLs so secrets never leak into UI, logs, or diagnostics."""
    try:
        url = urlsplit(str(value or ""))
        if not url.scheme or not url.netloc:
            return str(value or "")
        parts = [p for p in url.path.split("/") if p]
        if len(parts) >= 2:
            is_local = url.hostname in ("127.0.0.1", "localhost")
            key = parts[-1]
            if not is_local or len(key) > 16:
                masked_key = (key[:3] + "••••" + key[-2:]) if len(key) > 8 else "••••"
                parts[-1] = masked_key
        query = "?••••" if url.query else ""
        return f"{url.scheme}://{url.netloc}/" + "/".join(parts) + query
    except Exception:
        return "rtmp://••••"


def list_presets():
    return [dict(v) for v in PRESETS.values()]
