"""Local settings persistence with optional Windows DPAPI stream-key encryption."""
import base64
import json
import os
from pathlib import Path

from .paths import data_root


def _dpapi_protect(plaintext: str) -> str:
    if not plaintext:
        return ""
    if os.name != "nt":
        raise RuntimeError("El cifrado DPAPI solo está disponible en Windows.")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = plaintext.encode("utf-8")
    buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = DATA_BLOB(len(raw), buf)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), "OffsetStreamKey", None, None, None, 0, ctypes.byref(out_blob)):
        raise RuntimeError("No se pudo cifrar la clave con Windows DPAPI.")
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(encoded: str) -> str:
    if not encoded:
        return ""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = base64.b64decode(encoded.encode("ascii"))
    buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = DATA_BLOB(len(raw), buf)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        return ""
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)


class SettingsStore:
    """Persists streamer preferences safely in data/settings.json."""

    def __init__(self, path=None):
        self.path = Path(path) if path else data_root() / "settings.json"

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, updates: dict) -> dict:
        current = self.load()
        current.update(updates)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        return current

    def save_encrypted_key(self, preset: str, stream_key: str) -> bool:
        if not preset or preset in ("local", "custom"):
            return False
        data = self.load()
        keys = dict(data.get("encrypted_keys") or {})
        if not stream_key:
            keys.pop(preset, None)
        else:
            if os.name != "nt":
                return False
            keys[preset] = _dpapi_protect(stream_key)
        self.save({"encrypted_keys": keys})
        return True

    def load_encrypted_key(self, preset: str) -> str:
        data = self.load()
        keys = data.get("encrypted_keys") or {}
        enc = keys.get(preset)
        if not enc:
            return ""
        try:
            return _dpapi_unprotect(enc)
        except Exception:
            return ""

    def saved_key_presets(self) -> list:
        data = self.load()
        keys = data.get("encrypted_keys") or {}
        return sorted(k for k, v in keys.items() if v)
