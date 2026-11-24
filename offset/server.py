"""Loopback-only control plane: live dynamic delay, panic slate, purge, presets, hotkeys, and persistence."""
import base64
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .destinations import build_output_url, list_presets, mask_rtmp_url
from .hotkeys import HotkeyManager
from .relay import Config, Relay
from .slate import MAX_SLATE_IMAGE_BYTES, SlateStore

WEB = Path(__file__).resolve().parent.parent / "web"
TERMINAL = {"FINISHED", "STOPPED", "FAULT"}


class Busy(RuntimeError):
    pass


class Controller:
    def __init__(self, config, *, slate_store=None, settings_store=None):
        self.config = config
        self.relay = None
        self.slate_store = slate_store if slate_store is not None else SlateStore()
        self.settings_store = settings_store
        self.hotkeys = HotkeyManager(self._on_hotkey)
        self.lock = threading.Lock()
        self.operation_lock = threading.Lock()
        if self.settings_store is not None:
            self._load_persisted()

    def _load_persisted(self):
        saved = self.settings_store.load()
        if not saved:
            return
        updates = {}
        for key, conv in (
            ("default_delay", float),
            ("memory_mb", int),
            ("catchup_rate", float),
            ("auto_reconnect", bool),
            ("preset", str),
        ):
            if key in saved:
                try:
                    updates[key] = conv(saved[key])
                except (TypeError, ValueError):
                    pass
        if "local_output_url" in saved and updates.get("preset", self.config.preset) in ("local", "custom"):
            updates["output_url"] = str(saved["local_output_url"])
        try:
            candidate = replace(self.config, **updates)
            candidate.validate()
            self.config = candidate
        except Exception:
            pass
        hk = saved.get("hotkeys")
        if isinstance(hk, dict):
            try:
                self.hotkeys.configure(bool(hk.get("enabled", False)), hk.get("bindings"))
            except Exception:
                pass

    def _persist_config(self, cfg):
        if self.settings_store is None:
            return
        payload = {
            "preset": cfg.preset,
            "default_delay": cfg.default_delay,
            "memory_mb": cfg.memory_mb,
            "catchup_rate": cfg.catchup_rate,
            "auto_reconnect": cfg.auto_reconnect,
        }
        if cfg.preset in ("local", "custom"):
            payload["local_output_url"] = cfg.output_url
        try:
            self.settings_store.save(payload)
        except Exception:
            pass

    def _on_hotkey(self, action):
        with self.lock:
            relay = self.relay
        if not relay or relay.state in TERMINAL or relay.state == "READY":
            return
        try:
            if action == "toggle_delay":
                relay.toggle_delay(delay_s=self.config.default_delay, disable_mode="jump")
            elif action == "jump_live":
                relay.disable_delay(mode="jump")
            elif action == "catchup_live":
                relay.disable_delay(mode="catchup")
            elif action == "panic_slate":
                relay.toggle_panic_slate()
        except Exception:
            pass

    def status(self):
        with self.lock:
            relay = self.relay
            cfg = self.config
        if relay:
            snap = relay.snapshot()
        else:
            snap = {
                "state": "READY",
                "delay_mode": "delayed" if cfg.delay > 0 else "live",
                "error": None,
                "delay_target_s": cfg.delay,
                "default_delay_s": cfg.default_delay,
                "catchup_rate": cfg.catchup_rate,
                "catchup_eta_s": None,
                "last_packet_residence_s": None,
                "received_packets": 0,
                "sent_packets": 0,
                "slate_packets_sent": 0,
                "dropped_jump_packets": 0,
                "purged_packets": 0,
                "last_purge": None,
                "panic_active": False,
                "auto_reconnect": cfg.auto_reconnect,
                "reconnects": {"input": 0, "output": 0, "input_reconnecting": False, "output_reconnecting": False},
                "input_url": cfg.input_url,
                "output_url": mask_rtmp_url(cfg.output_url),
                "preset": cfg.preset,
                "buffer": {"bytes": 0, "peak_bytes": 0, "packets": 0,
                           "limit_bytes": cfg.memory_mb * 1024**2, "seconds": 0},
                "events": [],
                "memory": {},
                "stream": {"width": None, "height": None, "fps": None,
                           "sample_rate": None, "channels": None, "has_bframes": False},
                "slate": {"ready": False, "compiling": False, "is_custom": False,
                          "elapsed_s": 0.0, "target_s": 0.0, "progress": 0.0, "error": None},
                "capabilities": {
                    "fixed_delay": True, "dynamic_delay": True, "slate": True,
                    "panic_slate": True, "catchup": True, "jump_live": True,
                    "purge": True, "auto_reconnect": cfg.auto_reconnect,
                    "bframes_compatible": cfg.allow_bframes,
                    "remote_rtmp": cfg.allow_remote_output,
                },
            }
        snap["presets"] = list_presets()
        snap["slate_store"] = self.slate_store.info()
        snap["hotkeys"] = self.hotkeys.status()
        snap["saved_key_presets"] = self.settings_store.saved_key_presets() if self.settings_store else []
        return snap

    def start(self, values):
        allowed = {"delay", "default_delay", "memory_mb", "output_url",
                   "preset", "stream_key", "catchup_rate", "allow_bframes",
                   "auto_reconnect", "remember_key"}
        if set(values) - allowed:
            raise ValueError("Configuración desconocida.")
        with self.operation_lock:
            with self.lock:
                previous = self.relay
                base_cfg = self.config
            if previous and previous.state not in TERMINAL:
                raise ValueError("Detén la sesión antes de cambiar la conexión principal.")

            updates = {}
            if "delay" in values:
                updates["delay"] = float(values["delay"])
            if "default_delay" in values:
                updates["default_delay"] = float(values["default_delay"])
            if "memory_mb" in values:
                updates["memory_mb"] = int(values["memory_mb"])
            if "catchup_rate" in values:
                updates["catchup_rate"] = float(values["catchup_rate"])
            if "allow_bframes" in values:
                updates["allow_bframes"] = bool(values["allow_bframes"])
            if "auto_reconnect" in values:
                updates["auto_reconnect"] = bool(values["auto_reconnect"])

            preset = values.get("preset")
            stream_key = str(values.get("stream_key") or "").strip()
            custom_url = str(values.get("output_url") or "").strip()
            remember_key = bool(values.get("remember_key", False))

            if preset is not None or stream_key:
                chosen_preset = str(preset or base_cfg.preset or "local").strip().lower()
                if not stream_key and chosen_preset in ("twitch", "youtube", "kick") and self.settings_store:
                    stream_key = self.settings_store.load_encrypted_key(chosen_preset)
                updates["preset"] = chosen_preset
                updates["output_url"] = build_output_url(chosen_preset, stream_key=stream_key, custom_url=custom_url)
                if remember_key and stream_key and self.settings_store and chosen_preset in ("twitch", "youtube", "kick"):
                    try:
                        self.settings_store.save_encrypted_key(chosen_preset, stream_key)
                    except Exception:
                        pass
            elif "output_url" in values:
                updates["output_url"] = custom_url

            config = replace(base_cfg, **updates)
            config.validate()
            if previous:
                if not previous.stop():
                    raise Busy("El cierre de la sesión anterior sigue pendiente. Espera unos segundos y vuelve a intentarlo.")
            relay = Relay(config, slate_store=self.slate_store)
            with self.lock:
                self.config = config
                self.relay = relay
            self._persist_config(config)
            relay.start()
            return self.status()

    def stop(self):
        with self.operation_lock:
            with self.lock:
                relay = self.relay
            if relay:
                if not relay.stop():
                    raise Busy("El cierre de la sesión sigue pendiente. Espera unos segundos y vuelve a intentarlo.")
            return {"ok": True}

    def enable_delay(self, values):
        if set(values) - {"delay"}:
            raise ValueError("Parámetro desconocido.")
        with self.lock:
            relay = self.relay
            default_delay = self.config.default_delay
        delay_s = float(values.get("delay", default_delay))
        if not relay or relay.state in TERMINAL:
            if not 0.5 <= delay_s <= 120:
                raise ValueError("El retraso debe estar entre 0.5 y 120 segundos.")
            with self.lock:
                self.config = replace(self.config, default_delay=delay_s)
                cfg = self.config
            self._persist_config(cfg)
            return self.status()
        with self.lock:
            self.config = replace(self.config, default_delay=delay_s)
            cfg = self.config
        self._persist_config(cfg)
        relay.enable_delay(delay_s)
        return self.status()

    def set_delay(self, values):
        if set(values) - {"delay", "mode", "catchup_rate"}:
            raise ValueError("Parámetro desconocido.")
        if "delay" not in values:
            raise ValueError("Falta el parámetro delay.")
        with self.lock:
            relay = self.relay
        delay_s = float(values["delay"])
        if not relay or relay.state in TERMINAL:
            if delay_s > 0:
                return self.enable_delay({"delay": delay_s})
            return self.status()
        relay.set_delay(delay_s, mode=values.get("mode", "catchup"), catchup_rate=values.get("catchup_rate"))
        return self.status()

    def disable_delay(self, values):
        if set(values) - {"mode", "catchup_rate"}:
            raise ValueError("Parámetro desconocido.")
        with self.lock:
            relay = self.relay
        if not relay or relay.state in TERMINAL:
            raise ValueError("No hay una sesión activa.")
        relay.disable_delay(mode=values.get("mode", "jump"), catchup_rate=values.get("catchup_rate"))
        return self.status()

    def toggle_delay(self, values):
        if set(values) - {"delay", "disable_mode"}:
            raise ValueError("Parámetro desconocido.")
        with self.lock:
            relay = self.relay
            default_delay = self.config.default_delay
        if not relay or relay.state in TERMINAL:
            raise ValueError("Inicia la sesión antes de alternar el delay.")
        relay.toggle_delay(delay_s=values.get("delay", default_delay),
                           disable_mode=values.get("disable_mode", "jump"))
        return self.status()

    def panic_on(self):
        with self.lock:
            relay = self.relay
        if not relay or relay.state in TERMINAL:
            raise ValueError("No hay una sesión activa.")
        relay.hold_slate()
        return self.status()

    def panic_off(self, values):
        if set(values) - {"mode"}:
            raise ValueError("Parámetro desconocido.")
        with self.lock:
            relay = self.relay
        if not relay or relay.state in TERMINAL:
            raise ValueError("No hay una sesión activa.")
        relay.release_slate(mode=values.get("mode", "jump"))
        return self.status()

    def panic_toggle(self):
        with self.lock:
            relay = self.relay
        if not relay or relay.state in TERMINAL:
            raise ValueError("No hay una sesión activa.")
        relay.toggle_panic_slate()
        return self.status()

    def purge(self, values):
        if set(values) - {"seconds"}:
            raise ValueError("Parámetro desconocido.")
        with self.lock:
            relay = self.relay
        if not relay or relay.state in TERMINAL:
            raise ValueError("No hay una sesión activa.")
        secs = values.get("seconds")
        relay.purge(seconds=None if secs is None else float(secs))
        return self.status()

    def upload_slate(self, raw_bytes, filename="slate"):
        info = self.slate_store.save_image(raw_bytes, original_name=filename)
        with self.lock:
            relay = self.relay
        if relay and relay.state not in TERMINAL:
            relay.invalidate_slate()
        return {"ok": True, "slate_store": info}

    def reset_slate(self):
        info = self.slate_store.delete_image()
        with self.lock:
            relay = self.relay
        if relay and relay.state not in TERMINAL:
            relay.invalidate_slate()
        return {"ok": True, "slate_store": info}

    def configure_hotkeys(self, values):
        if set(values) - {"enabled", "bindings"}:
            raise ValueError("Parámetros de atajos desconocidos.")
        enabled = bool(values.get("enabled", False))
        bindings = values.get("bindings")
        st = self.hotkeys.configure(enabled, bindings)
        if self.settings_store is not None:
            try:
                self.settings_store.save({"hotkeys": {"enabled": st["enabled"], "bindings": st["bindings"]}})
            except Exception:
                pass
        return {"ok": True, "hotkeys": st}

    def shutdown(self):
        self.hotkeys.stop()
        try:
            self.stop()
        except Busy:
            pass


def make_server(config, port=8765, *, slate_store=None, settings_store=None):
    controller = Controller(config, slate_store=slate_store, settings_store=settings_store)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Offset"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def send_data(self, status, data, mime="application/json"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            if mime.startswith("text/") or mime == "application/json":
                self.send_header("Content-Type", mime + "; charset=utf-8")
            else:
                self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "font-src 'self'; img-src 'self' blob: data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def trusted(self, api=False):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host:
                self.send_data(403, {"error": "Abre el panel usando 127.0.0.1."}); return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                self.send_data(403, {"error": "Origen no autorizado."}); return False
            if api and not secrets.compare_digest(self.headers.get("X-Offset-Token", ""), token):
                self.send_data(403, {"error": "Vuelve a abrir el panel local."}); return False
            return True

        def do_GET(self):
            route = urlsplit(self.path).path
            if not self.trusted(route.startswith("/api/")):
                return
            if route == "/api/status":
                self.send_data(200, controller.status())
            elif route == "/slate-preview":
                data, mime = controller.slate_store.read_image()
                if data is None:
                    self.send_data(404, {"error": "Sin imagen personalizada."})
                else:
                    self.send_data(200, data, mime)
            elif route == "/fonts/PlusJakartaSans-Variable.woff2":
                font_file = WEB / "fonts" / "PlusJakartaSans-Variable.woff2"
                if font_file.is_file():
                    self.send_data(200, font_file.read_bytes(), "font/woff2")
                else:
                    self.send_data(404, {"error": "Fuente no encontrada."})
            elif route in ("/", "/app.js", "/style.css"):
                filename, mime = {
                    "/": ("index.html", "text/html"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/style.css": ("style.css", "text/css"),
                }[route]
                data = (WEB / filename).read_bytes()
                if route == "/":
                    data = data.replace(b"__TOKEN__", token.encode())
                self.send_data(200, data, mime)
            else:
                self.send_data(404, {"error": "No encontrado."})

        def do_POST(self):
            route = urlsplit(self.path).path
            if not self.trusted(True):
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                ctype = self.headers.get_content_type()

                if route == "/api/slate" and ctype in ("image/png", "image/jpeg", "image/webp", "application/octet-stream"):
                    if not 0 < size <= MAX_SLATE_IMAGE_BYTES:
                        raise ValueError("La imagen debe pesar menos de 10 MiB.")
                    raw = self.rfile.read(size)
                    fname = self.headers.get("X-Slate-Filename", "slate")
                    self.send_data(200, controller.upload_slate(raw, filename=fname))
                    return

                max_json = 15 * 1024 * 1024 if route == "/api/slate" else 8192
                if not 0 < size <= max_json or ctype != "application/json":
                    raise ValueError("Petición inválida.")
                values = json.loads(
                    self.rfile.read(size),
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Número inválido.")),
                )
                if not isinstance(values, dict):
                    raise ValueError("Se esperaba un objeto JSON.")

                if route == "/api/start":
                    result = controller.start(values)
                elif route == "/api/stop":
                    result = controller.stop()
                elif route == "/api/delay/enable":
                    result = controller.enable_delay(values)
                elif route == "/api/delay/set":
                    result = controller.set_delay(values)
                elif route == "/api/delay/disable":
                    result = controller.disable_delay(values)
                elif route == "/api/delay/toggle":
                    result = controller.toggle_delay(values)
                elif route == "/api/panic/on":
                    result = controller.panic_on()
                elif route == "/api/panic/off":
                    result = controller.panic_off(values)
                elif route == "/api/panic/toggle":
                    result = controller.panic_toggle()
                elif route == "/api/purge":
                    result = controller.purge(values)
                elif route == "/api/slate":
                    b64 = values.get("data_base64")
                    if not isinstance(b64, str) or not b64:
                        raise ValueError("Falta la imagen codificada.")
                    raw = base64.b64decode(b64, validate=True)
                    result = controller.upload_slate(raw, filename=values.get("filename", "slate"))
                elif route == "/api/slate/reset":
                    result = controller.reset_slate()
                elif route == "/api/hotkeys":
                    result = controller.configure_hotkeys(values)
                else:
                    self.send_data(404, {"error": "Acción no disponible."}); return
                self.send_data(200, result)
            except Busy as exc:
                self.send_data(409, {"error": str(exc)})
            except (ValueError, TypeError, OverflowError) as exc:
                msg = str(exc) or "Revisa los valores introducidos."
                self.send_data(400, {"error": msg})
            except Exception:
                self.send_data(500, {"error": "No se pudo completar la acción. Comprueba FFmpeg y el destino."})

        def do_DELETE(self):
            route = urlsplit(self.path).path
            if not self.trusted(True):
                return
            if route == "/api/slate":
                self.send_data(200, controller.reset_slate())
            else:
                self.send_data(404, {"error": "Acción no disponible."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.controller = controller
    return server
