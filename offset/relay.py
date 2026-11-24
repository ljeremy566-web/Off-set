"""Dynamic RTMP relay via two FFmpeg stream-copy subprocesses.

Supports live delay ON/OFF with zero lost frames on activation (custom slate
loop + exact IDR/AAC splice), clean jump-to-live (next IDR), lossless gradual
catch-up, immediate panic slate hold, anti-leak buffer purge, elastic delay
adjustments, optional input/output auto-reconnect, and native Windows/Linux stats.
"""
from collections import deque
from dataclasses import dataclass, replace
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import __version__
from .buffer import PacketBuffer, Pacer
from .destinations import local_rtmp, mask_rtmp_url, validate_output_rtmp
from .media import FLV_HEADER, Validator, read_tags, tag_has_idr
from .paths import resolve_binary
from .slate import SlateCursor, SlateStore, compile_slate, probe_stream_format


@dataclass(frozen=True)
class Config:
    delay: float = 10
    memory_mb: int = 128
    input_url: str = "rtmp://127.0.0.1:1935/live/input"
    output_url: str = "rtmp://127.0.0.1:1936/live/offset"
    ffmpeg: str = "ffmpeg"
    stall_timeout: float = 10
    allow_bframes: bool = True
    allow_remote_output: bool = True
    preset: str = "local"
    default_delay: float = 30.0
    catchup_rate: float = 1.25
    slate_image: str | None = None
    auto_reconnect: bool = False
    reconnect_timeout: float = 60.0
    output_retries: int = 3

    def validate(self):
        if sys.version_info < (3, 11):
            raise ValueError("Offset requiere Python 3.11 o posterior.")
        if isinstance(self.delay, bool) or not math.isfinite(self.delay) or not 0 <= self.delay <= 120:
            raise ValueError("El retraso debe estar entre 0 y 120 segundos.")
        if (isinstance(self.default_delay, bool) or not math.isfinite(self.default_delay)
                or not 1 <= self.default_delay <= 120):
            raise ValueError("El retraso objetivo debe estar entre 1 y 120 segundos.")
        if (isinstance(self.catchup_rate, bool) or not math.isfinite(self.catchup_rate)
                or not 1.05 <= self.catchup_rate <= 1.60):
            raise ValueError("La velocidad de alcance debe estar entre 1.05x y 1.60x.")
        if type(self.memory_mb) is not int or not 8 <= self.memory_mb <= 512:
            raise ValueError("El buffer debe tener entre 8 y 512 MiB.")
        local_rtmp(self.input_url)
        validate_output_rtmp(self.output_url, allow_remote=self.allow_remote_output)
        in_url, out_url = urlsplit(self.input_url), urlsplit(self.output_url)
        if out_url.hostname in ("127.0.0.1", "localhost") and in_url.port == out_url.port:
            raise ValueError("Entrada y destino deben usar puertos distintos.")
        ff_bin = resolve_binary(self.ffmpeg)
        if not (Path(ff_bin).is_file() or shutil.which(ff_bin)):
            raise ValueError("FFmpeg no está en PATH ni junto a Offset. Instálalo antes de iniciar.")
        if not 1 <= self.stall_timeout <= 60:
            raise ValueError("Timeout inválido.")
        if not 2 <= self.reconnect_timeout <= 300:
            raise ValueError("Timeout de reconexión inválido.")
        if type(self.output_retries) is not int or not 0 <= self.output_retries <= 10:
            raise ValueError("Número de reintentos de salida inválido.")


def _windows_process_stats(pids):
    """Native Win32 RSS (WorkingSetSize) and CPU seconds via stdlib ctypes."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    get_mem = getattr(kernel32, "K32GetProcessMemoryInfo", None) or (
        getattr(psapi, "GetProcessMemoryInfo", None) if psapi else None
    )
    if get_mem is None:
        return None, None, None, None

    total_rss = 0
    total_cpu = 0.0
    own_peak = None
    own_cpu = None
    self_pid = os.getpid()

    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid))
        if not handle:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None, None, own_peak, own_cpu
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if not get_mem(handle, ctypes.byref(counters), counters.cb):
                return None, None, own_peak, own_cpu
            total_rss += int(counters.WorkingSetSize)

            creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                            ctypes.byref(kernel_t), ctypes.byref(user_t)):
                return None, None, own_peak, own_cpu
            kt = (kernel_t.dwHighDateTime << 32) | kernel_t.dwLowDateTime
            ut = (user_t.dwHighDateTime << 32) | user_t.dwLowDateTime
            cpu_s = (kt + ut) / 10_000_000.0
            total_cpu += cpu_s
            if int(pid) == self_pid:
                own_peak = int(counters.PeakWorkingSetSize)
                own_cpu = cpu_s
        finally:
            kernel32.CloseHandle(handle)
    return total_rss, total_cpu, own_peak, own_cpu


def process_stats(pids):
    """Current RSS/CPU for the relay plus its transport children (Windows & Linux)."""
    if os.name == "nt":
        try:
            rss, cpu, own_peak, own_cpu = _windows_process_stats(pids)
            return {
                "rss_bytes": rss,
                "cpu_seconds": cpu,
                "python_peak_rss_bytes": own_peak,
                "python_cpu_seconds": own_cpu,
            }
        except Exception:
            return {"rss_bytes": None, "cpu_seconds": None,
                    "python_peak_rss_bytes": None, "python_cpu_seconds": None}

    own_peak = own_cpu = None
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        own_peak = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        own_cpu = usage.ru_utime + usage.ru_stime
    except (ImportError, AttributeError):
        pass
    base = {"rss_bytes": None, "cpu_seconds": None,
            "python_peak_rss_bytes": own_peak, "python_cpu_seconds": own_cpu}
    try:
        if int(Path("/proc/self/stat").read_text().split()[0]) != os.getpid():
            return base
    except (OSError, ValueError):
        return base
    rss, cpu = 0, 0.0
    for pid in pids:
        try:
            stats = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            rss += int(stats[21]) * os.sysconf("SC_PAGE_SIZE")
            cpu += (int(stats[11]) + int(stats[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, ValueError):
            return base
    return {**base, "rss_bytes": rss, "cpu_seconds": cpu}


class Relay:
    def __init__(self, config, *, input_file=None, output_file=None, audit_file=None, slate_store=None):
        config.validate()
        self.config = config
        self.input_file = Path(input_file) if input_file else None
        self.output_file = Path(output_file) if output_file else None
        self.audit_file = Path(audit_file) if audit_file else None
        self.slate_store = slate_store if slate_store is not None else SlateStore()
        max_buffer_s = max(config.delay, config.default_delay, 120.0) + 30.0
        self.buffer = PacketBuffer(config.memory_mb * 1024**2, max_buffer_s)
        self.validator = Validator(allow_bframes=config.allow_bframes)
        self.stop_event = threading.Event()
        self._stop_requested = False
        self.done = threading.Event()
        self.ingress_done = threading.Event()
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.state = "READY"
        self.events = deque(maxlen=80)
        self.error = None
        self.input_proc = self.output_proc = None
        self.threads = []
        self.egress_thread = None
        self.received = self.sent = 0
        self.dropped_jump_packets = 0
        self.purged_packets = 0
        self.slate_packets_sent = 0
        self.last_age = self.first_arrival = self.write_started = None
        self.last_input = None
        self.last_progress = None
        self.last_sent_ts = None
        self.started = None
        self.transport_diagnostics = deque(maxlen=16)
        self.rss_peak = 0
        self.audit_count = 0

        # Dynamic delay & Hito A3 state
        self.target_delay = float(config.delay)
        self.delay_mode = "delayed" if config.delay > 0 else "live"
        self.catchup_rate = float(config.catchup_rate)
        self._pending_cmd = None
        self._cmd_event = threading.Event()
        self._slate_elapsed_s = 0.0
        self._slate_target_s = 0.0
        self._panic_active = False
        self.last_purge = None

        # Auto-reconnect / epoch state
        self.input_epoch = 0
        self.input_reconnects = 0
        self.output_reconnects = 0
        self._input_reconnecting = False
        self._input_reconnect_since = None
        self._input_reconnect_timed_out = False
        self._output_reconnecting = False
        self._epoch_boundaries = {}  # shifted_idr_ts -> {9: avc_header, 8: aac_header}
        self._ingress_wait_idr = False
        self._splice_idrs = set()  # shifted_idr_ts requiring clean timestamp rebase after purge

        # Background slate preparation
        self._stream_fmt = None
        self._compiled_slate = None
        self._slate_error = None
        self._slate_compiling = False
        self._slate_ready_event = threading.Event()
        self._slate_started = False

    def event(self, message):
        with self.lock:
            self.events.append({"at": time.strftime("%H:%M:%S"), "message": message})

    def set_state(self, state, allowed_from=None):
        with self.lock:
            if (self.state in {"FAULT", "STOPPED", "FINISHED"} or self.stop_event.is_set()
                    or (allowed_from is not None and self.state not in allowed_from)):
                return False
            self.state = state
            return True

    def _spawn(self, command, attribute):
        with self.lifecycle_lock:
            if self.stop_event.is_set():
                raise RuntimeError("La sesión ya se está cerrando.")
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0)
            setattr(self, attribute, proc)
            thread = threading.Thread(target=self._stderr, args=(proc,), daemon=True)
            thread.start(); self.threads.append(thread)
            return proc

    def _close_stream(self, stream, *, fatal=True):
        if stream is None:
            return
        try:
            stream.close()
        except Exception:
            if fatal and not self._stop_requested:
                self.fail("No se pudo cerrar un transporte o archivo de diagnóstico.")
            else:
                self.event("No se pudo cerrar un recurso local; el contenido ya procesado no se modifica.")

    def _stderr(self, proc):
        try:
            while (line := proc.stderr.readline(2048)):
                if b"error" in line.lower() or b"failed" in line.lower() or b"refused" in line.lower():
                    with self.lock:
                        self.transport_diagnostics.append("FFmpeg informó de un error de transporte.")
        except (OSError, ValueError):
            if not self.stop_event.is_set() and not self._input_reconnecting and not self._output_reconnecting:
                self.fail("No se pudo leer el diagnóstico del transporte.")
        finally:
            self._close_stream(proc.stderr, fatal=not (self._input_reconnecting or self._output_reconnecting))

    def start(self):
        with self.lifecycle_lock:
            self._start_locked()

    def _start_locked(self):
        with self.lock:
            if self.started is not None or self.stop_event.is_set():
                raise RuntimeError("Esta sesión ya se inició.")
            self.started = time.perf_counter()
            self.state = "WAITING_INPUT"
            mode_msg = f"retraso inicial de {self.target_delay:g} s" if self.target_delay > 0 else "en directo (0 s)"
            self.event(f"Sesión lista ({mode_msg}). Esperando señal de OBS.")
        try:
            if self.input_file is None:
                self._spawn(self.input_command(), "input_proc")
            for target in (self._ingress, self._egress, self._watchdog):
                if self.stop_event.is_set():
                    break
                thread = threading.Thread(target=target, daemon=True)
                if target == self._egress:
                    self.egress_thread = thread
                thread.start(); self.threads.append(thread)
        except Exception:
            self.fail("No se pudo iniciar FFmpeg o sus tareas.")
            raise

    def input_command(self, rw_timeout_s=None):
        timeout = self.config.stall_timeout if rw_timeout_s is None else rw_timeout_s
        return [resolve_binary(self.config.ffmpeg), "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-rw_timeout", str(int(timeout * 1e6)),
                "-listen", "1", "-probesize", "32768", "-analyzeduration", "500000",
                "-i", self.config.input_url, "-map", "0:v:0", "-map", "0:a:0",
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-max_interleave_delta", "100000", "-flush_packets", "1",
                "-flvflags", "no_duration_filesize", "-f", "flv", "pipe:1"]

    def output_command(self):
        return [resolve_binary(self.config.ffmpeg), "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-probesize", "32768", "-analyzeduration", "500000", "-f", "flv",
                "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                "-copyts", "-avoid_negative_ts", "disabled", "-flush_packets", "1",
                "-max_interleave_delta", "100000", "-flvflags", "no_duration_filesize",
                "-rw_timeout", str(int(self.config.stall_timeout * 1e6)),
                "-f", "flv", self.config.output_url]

    # --- Background slate preparation ---

    def _resolve_slate_image(self):
        if self.config.slate_image:
            p = Path(self.config.slate_image)
            if p.is_file():
                return p
        if self.slate_store:
            return self.slate_store.get_image_path()
        return None

    def _maybe_prepare_slate(self):
        with self.lock:
            if self._slate_started or not self.validator.ready:
                return
            if len(self.validator.video_dts_samples) < 4 and not self.ingress_done.is_set():
                return
            avc = self.validator.headers.get(9)
            aac = self.validator.headers.get(8)
            idr = self.validator.first_idr_tag
            sr = self.validator.sample_rate
            ch = self.validator.channels
            dts_samples = tuple(self.validator.video_dts_samples)
            if not (avc and aac and idr):
                return
            self._slate_started = True
            self._slate_compiling = True

        def worker():
            try:
                fmt = probe_stream_format(avc, aac, idr, sr, ch, dts_samples, ffmpeg=self.config.ffmpeg)
                img = self._resolve_slate_image()
                compiled = compile_slate(fmt, image_path=img, ffmpeg=self.config.ffmpeg, duration_s=2)
                with self.lock:
                    self._stream_fmt = fmt
                    self._compiled_slate = compiled
                    self._slate_error = None
            except Exception as exc:
                with self.lock:
                    self._slate_error = str(exc)
            finally:
                with self.lock:
                    self._slate_compiling = False
                self._slate_ready_event.set()

        thread = threading.Thread(target=worker, daemon=True, name="offset-slate-build")
        with self.lifecycle_lock:
            if not self.stop_event.is_set():
                thread.start()
                self.threads.append(thread)

    def invalidate_slate(self):
        with self.lock:
            self._compiled_slate = None
            self._slate_started = False
            self._slate_ready_event.clear()
        self._maybe_prepare_slate()

    def _get_compiled_slate(self, timeout=10.0):
        self._maybe_prepare_slate()
        if not self._slate_ready_event.is_set():
            with self.lock:
                if not self._slate_started and self.validator.ready:
                    avc = self.validator.headers.get(9)
                    aac = self.validator.headers.get(8)
                    idr = self.validator.first_idr_tag
                    sr = self.validator.sample_rate
                    ch = self.validator.channels
                    dts_samples = tuple(self.validator.video_dts_samples)
                    self._slate_started = True
                    self._slate_compiling = True
                else:
                    avc = None
            if avc is not None:
                try:
                    fmt = probe_stream_format(avc, aac, idr, sr, ch, dts_samples, ffmpeg=self.config.ffmpeg)
                    img = self._resolve_slate_image()
                    compiled = compile_slate(fmt, image_path=img, ffmpeg=self.config.ffmpeg, duration_s=2)
                    with self.lock:
                        self._stream_fmt = fmt
                        self._compiled_slate = compiled
                        self._slate_error = None
                except Exception as exc:
                    with self.lock:
                        self._slate_error = str(exc)
                finally:
                    with self.lock:
                        self._slate_compiling = False
                    self._slate_ready_event.set()
            else:
                deadline = time.perf_counter() + timeout
                while not self._slate_ready_event.is_set() and not self.stop_event.is_set():
                    if time.perf_counter() >= deadline:
                        break
                    self._slate_ready_event.wait(0.05)
        with self.lock:
            return self._compiled_slate, self._slate_error

    # --- Dynamic Delay, Panic Slate & Anti-Leak Purge Live Control API ---

    def enable_delay(self, delay_s):
        """Activate or increase live delay to `delay_s` seconds with 0 lost OBS frames."""
        if isinstance(delay_s, bool) or not math.isfinite(float(delay_s)) or not 0.5 <= float(delay_s) <= 120:
            raise ValueError("El retraso dinámico debe estar entre 0.5 y 120 segundos.")
        delay_s = float(delay_s)
        with self.lock:
            if self.state in {"FAULT", "STOPPED", "FINISHED", "DRAINING"}:
                raise ValueError("No se puede activar delay en una sesión inactiva o finalizando.")
            self.buffer.set_max_seconds(delay_s + 30.0)
            self.target_delay = delay_s
            self._panic_active = False
            if self.state in {"READY", "WAITING_INPUT", "BUFFERING"}:
                self.delay_mode = "delayed"
                self.event(f"Retraso ajustado a {delay_s:g} s.")
                return self.snapshot()
            self._pending_cmd = ("enable_delay", delay_s)
            self._cmd_event.set()
            self.event(f"Activando delay de {delay_s:g} s con slate (0 frames perdidos)...")
        return self.snapshot()

    def set_delay(self, delay_s, mode="catchup", catchup_rate=None):
        """Elastic delay adjustment: increase via slate or decrease via catch-up/jump."""
        if isinstance(delay_s, bool) or not math.isfinite(float(delay_s)) or not 0 <= float(delay_s) <= 120:
            raise ValueError("El retraso debe estar entre 0 y 120 segundos.")
        delay_s = float(delay_s)
        if delay_s == 0:
            return self.disable_delay(mode=mode, catchup_rate=catchup_rate)
        with self.lock:
            current = self.target_delay
        if delay_s >= current:
            return self.enable_delay(delay_s)
        mode = str(mode or "catchup").strip().lower()
        if mode not in ("jump", "catchup"):
            raise ValueError("Modo inválido. Usa 'jump' o 'catchup'.")
        rate = self.catchup_rate
        if catchup_rate is not None:
            if isinstance(catchup_rate, bool) or not math.isfinite(float(catchup_rate)) or not 1.05 <= float(catchup_rate) <= 1.60:
                raise ValueError("La velocidad de alcance debe estar entre 1.05x y 1.60x.")
            rate = float(catchup_rate)
        with self.lock:
            if self.state in {"FAULT", "STOPPED", "FINISHED"}:
                raise ValueError("No hay una sesión activa.")
            self.target_delay = delay_s
            self.catchup_rate = rate
            self._panic_active = False
            if mode == "jump":
                self._pending_cmd = ("jump_to_delay", delay_s)
                self.event(f"Bajando delay a {delay_s:g} s (salto limpio al IDR)...")
            else:
                self._pending_cmd = ("catchup_to_delay", (delay_s, rate))
                self.event(f"Bajando delay a {delay_s:g} s a {rate:.2f}x sin perder frames...")
            self._cmd_event.set()
        return self.snapshot()

    def disable_delay(self, mode="jump", catchup_rate=None):
        """Return to live (0 s): mode='jump' (next IDR) or mode='catchup' (lossless)."""
        mode = str(mode or "jump").strip().lower()
        if mode not in ("jump", "catchup"):
            raise ValueError("Modo inválido. Usa 'jump' (Directo ya) o 'catchup' (Sin perder nada).")
        rate = self.catchup_rate
        if catchup_rate is not None:
            if isinstance(catchup_rate, bool) or not math.isfinite(float(catchup_rate)) or not 1.05 <= float(catchup_rate) <= 1.60:
                raise ValueError("La velocidad de alcance debe estar entre 1.05x y 1.60x.")
            rate = float(catchup_rate)
        with self.lock:
            if self.state in {"FAULT", "STOPPED", "FINISHED"}:
                raise ValueError("No hay una sesión activa.")
            self.target_delay = 0.0
            self.catchup_rate = rate
            self._panic_active = False
            if self.state in {"READY", "WAITING_INPUT", "BUFFERING"}:
                self.delay_mode = "live"
                self._pending_cmd = ("jump_live", 0.0) if mode == "jump" else ("catchup_live", rate)
                self._cmd_event.set()
                self.event("Delay desactivado: saldrá en directo (0 s).")
                return self.snapshot()
            if mode == "jump":
                self._pending_cmd = ("jump_live", 0.0)
                self.event("Saltando a directo en el próximo keyframe IDR...")
            else:
                self._pending_cmd = ("catchup_live", rate)
                self.event(f"Alcanzando directo a {rate:.2f}x sin perder frames...")
            self._cmd_event.set()
        return self.snapshot()

    def hold_slate(self):
        """Immediate panic protection: switch output to custom safe slate until released."""
        with self.lock:
            if self.state in {"FAULT", "STOPPED", "FINISHED"}:
                raise ValueError("No hay una sesión activa.")
            self._panic_active = True
            self._pending_cmd = ("panic_hold", True)
            self._cmd_event.set()
            self.event("PANTALLA SEGURA activada: mostrando imagen de espera sin cortar el directo.")
        return self.snapshot()

    def release_slate(self, mode="jump"):
        """Exit panic slate: 'jump' returns live discarding panic period; 'resume' continues buffered delay."""
        mode = str(mode or "jump").strip().lower()
        if mode not in ("jump", "resume"):
            raise ValueError("Modo inválido. Usa 'jump' (Directo ya) o 'resume' (Reanudar con buffer).")
        with self.lock:
            if self.state in {"FAULT", "STOPPED", "FINISHED"}:
                raise ValueError("No hay una sesión activa.")
            self._panic_active = False
            if mode == "jump":
                self.target_delay = 0.0
                self._pending_cmd = ("jump_live", 0.0)
                self.event("Saliendo de pantalla segura hacia directo (0 s)...")
            else:
                self._pending_cmd = ("panic_resume", self.target_delay)
                self.event("Reanudando transmisión desde el buffer...")
            self._cmd_event.set()
        return self.snapshot()

    def toggle_panic_slate(self):
        with self.lock:
            active = self._panic_active or self.delay_mode == "slate_hold"
        if active:
            return self.release_slate(mode="jump")
        return self.hold_slate()

    def purge(self, seconds=None):
        """Anti-leak purge: erase the most recent `seconds` (or all queued content) from the buffer.

        Note: bytes already handed to the output FFmpeg pipe cannot be recalled.
        """
        if seconds is not None:
            if isinstance(seconds, bool) or not math.isfinite(float(seconds)) or not 1 <= float(seconds) <= 120:
                raise ValueError("Los segundos a purgar deben estar entre 1 y 120.")
            seconds = float(seconds)
        with self.lock:
            if self.state in {"FAULT", "STOPPED", "FINISHED"}:
                raise ValueError("No hay una sesión activa.")
            nal_len = self.validator.nal_length
        if seconds is None:
            purged_pkts, purged_s = self.buffer.purge_all()
            emptied = True
        else:
            purged_pkts, purged_s, emptied = self.buffer.purge_recent(
                seconds, lambda t: tag_has_idr(t, nal_len)
            )
        with self.lock:
            self.purged_packets += purged_pkts
            # Discard incoming orphaned tail frames until OBS sends the next clean IDR.
            self._ingress_wait_idr = True
            self.last_purge = {
                "at": time.strftime("%H:%M:%S"),
                "requested_s": seconds,
                "purged_seconds": round(purged_s, 2),
                "purged_packets": purged_pkts,
                "emptied_buffer": emptied,
            }
            if emptied and self.state == "RELAYING":
                self._pending_cmd = ("post_purge_resync", self.target_delay)
                self._cmd_event.set()
            scope = f"últimos {seconds:g} s" if seconds is not None else "todo el buffer"
            self.event(
                f"PURGA ({scope}): eliminados {purged_pkts} paquetes ({purged_s:.1f} s pendientes). "
                "Lo ya entregado al transporte no puede recuperarse."
            )
        return self.snapshot()

    def toggle_delay(self, delay_s=None, disable_mode="jump"):
        with self.lock:
            is_live = (self.delay_mode == "live" and self.target_delay <= 0)
        if is_live:
            return self.enable_delay(delay_s if delay_s is not None else self.config.default_delay)
        return self.disable_delay(mode=disable_mode)

    def _pop_command(self):
        with self.lock:
            cmd = self._pending_cmd
            self._pending_cmd = None
            self._cmd_event.clear()
            return cmd

    def _peek_command(self):
        with self.lock:
            return self._pending_cmd

    # --- Ingress (with optional multi-epoch OBS auto-reconnect) ---

    def _terminate_proc(self, proc):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.5)
        except OSError:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            self._close_stream(pipe, fatal=False)

    def _ingress(self):
        stream = None
        clean_eof = False
        epoch_ts_shift_ms = 0
        highest_ts_ms = 0

        try:
            while not self.stop_event.is_set():
                epoch_validator = self.validator if self.input_epoch == 0 else Validator(allow_bframes=self.config.allow_bframes)
                epoch_boundary_recorded = (self.input_epoch == 0)
                stream = self.input_file.open("rb") if self.input_file else self.input_proc.stdout
                file_anchor = file_ts = None
                epoch_error = None
                epoch_had_media = False

                try:
                    for tag in read_tags(stream):
                        if self.stop_event.is_set():
                            break
                        if self.input_file and tag.kind in (8, 9):
                            if file_anchor is None:
                                file_anchor, file_ts = time.perf_counter(), tag.timestamp
                            if self._wait_until(file_anchor + (tag.timestamp - file_ts) / 1000):
                                break
                            tag = replace(tag, arrival=time.perf_counter())

                        with self.lock:
                            self.last_input = time.perf_counter()
                            keep = epoch_validator.accept(tag)
                            if self.input_epoch > 0 and epoch_validator.ready and self._input_reconnecting:
                                self.validator = epoch_validator
                                self._input_reconnecting = False
                                self._input_reconnect_since = None
                                self.input_reconnects += 1
                                self.event(f"OBS reconectado (época #{self.input_epoch + 1}). Reanudando señal real.")

                        if keep:
                            shifted_ts = tag.timestamp + epoch_ts_shift_ms
                            out_tag = tag if epoch_ts_shift_ms == 0 else tag.with_timestamp(shifted_ts)
                            highest_ts_ms = max(highest_ts_ms, shifted_ts)
                            epoch_had_media = True

                            with self.lock:
                                wait_idr = self._ingress_wait_idr
                            if wait_idr:
                                if epoch_validator.last_is_idr:
                                    with self.lock:
                                        self._ingress_wait_idr = False
                                        self._splice_idrs.add(shifted_ts)
                                else:
                                    continue

                            if not epoch_boundary_recorded and epoch_validator.ready:
                                with self.lock:
                                    self._epoch_boundaries[shifted_ts] = dict(epoch_validator.headers)
                                epoch_boundary_recorded = True

                            # During explicit panic hold, avoid filling memory to FAULT by trimming oldest GOPs
                            if self._panic_active:
                                snap = self.buffer.snapshot()
                                if snap["bytes"] > self.buffer.max_bytes * 0.80:
                                    nal_len = epoch_validator.nal_length
                                    self.buffer.drop_until_latest_idr(lambda t: tag_has_idr(t, nal_len))

                            if not self.buffer.put(out_tag):
                                break
                            with self.lock:
                                if self.first_arrival is None:
                                    self.first_arrival = out_tag.arrival
                                self.received += 1
                            if self.set_state("BUFFERING", {"WAITING_INPUT"}):
                                if self.target_delay > 0:
                                    self.event(f"Entrada recibida. Acumulando {self.target_delay:g} s de retraso.")
                                else:
                                    self.event("Entrada recibida. Iniciando transmisión en directo (0 s).")
                            if self.validator.ready and not self._slate_started and len(self.validator.video_dts_samples) >= 4:
                                self._maybe_prepare_slate()
                except Exception as exc:
                    epoch_error = exc

                if self.stop_event.is_set():
                    break

                # Decide whether to auto-reconnect OBS input or conclude normally/fault
                can_reconnect_input = (
                    self.config.auto_reconnect
                    and self.input_file is None
                    and self.validator.ready
                    and self.state in {"BUFFERING", "RELAYING"}
                )

                if can_reconnect_input:
                    old_proc = self.input_proc
                    with self.lock:
                        timed_out = self._input_reconnect_timed_out
                    if timed_out:
                        with self.lock:
                            self._input_reconnecting = False
                            self._input_reconnect_since = None
                        self._close_stream(stream, fatal=False)
                        stream = None
                        self._terminate_proc(old_proc)
                        clean_eof = True
                        if self.set_state("DRAINING", {"BUFFERING", "RELAYING"}):
                            self.event("OBS no reconectó dentro del tiempo de espera. Finalizando sesión.")
                        break
                    with self.lock:
                        self._input_reconnecting = True
                        if self._input_reconnect_since is None:
                            self._input_reconnect_since = time.perf_counter()
                    self._cmd_event.set()
                    if epoch_had_media:
                        self.event(
                            f"Entrada OBS interrumpida. Manteniendo directo con slate (esperando hasta "
                            f"{int(self.config.reconnect_timeout)} s)..."
                        )
                    self._close_stream(stream, fatal=False)
                    stream = None
                    self._terminate_proc(old_proc)

                    if self.stop_event.wait(0.25):
                        break

                    epoch_ts_shift_ms = highest_ts_ms + 1000
                    self.input_epoch += 1
                    try:
                        self._spawn(self.input_command(rw_timeout_s=self.config.reconnect_timeout), "input_proc")
                    except Exception:
                        if not self.stop_event.is_set():
                            self.fail("No se pudo reiniciar el listener RTMP para reconexión de OBS.")
                        break
                    continue

                # Standard single-epoch path (original behavior preserved 100%)
                if epoch_error is not None:
                    raise epoch_error
                if self.input_proc and self.input_proc.wait(timeout=3) != 0:
                    raise RuntimeError("La entrada RTMP terminó con un error.")
                if not self.validator.ready:
                    raise RuntimeError("No se recibió una entrada completa con IDR y AAC.")
                clean_eof = True
                if self.set_state("DRAINING", {"BUFFERING", "RELAYING"}):
                    self.event("La fuente terminó. Se enviará el contenido pendiente a velocidad normal.")
                break

        except Exception as exc:
            if not self.stop_event.is_set():
                self.fail(str(exc) if isinstance(exc, (ValueError, RuntimeError))
                          else "Fallo al leer la entrada. Revisa OBS y FFmpeg.")
        finally:
            with self.lock:
                self._input_reconnecting = False
            try:
                self._close_stream(stream, fatal=not clean_eof)
            finally:
                self.ingress_done.set()
                self.buffer.close()
                self._cmd_event.set()

    def _write(self, stream, data):
        with self.lock:
            self.write_started = time.perf_counter()
        try:
            view = memoryview(data)
            while view:
                count = stream.write(view)
                if not count:
                    raise BrokenPipeError()
                view = view[count:]
        finally:
            with self.lock:
                self.write_started = None

    def _wait_until(self, deadline, *, interrupt_on_cmd=False):
        while not self.stop_event.is_set():
            if interrupt_on_cmd and self._peek_command() is not None:
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return False
            if os.name == "nt":
                time.sleep(min(remaining, .005))
            else:
                if interrupt_on_cmd:
                    self.stop_event.wait(min(remaining, .01))
                else:
                    self.stop_event.wait(remaining)
        return True

    def _egress(self):
        stream = audit = None
        completed = False
        pacer = Pacer(self.target_delay)

        ts_offset_ms = 0
        last_out_v = None
        last_out_v_pts = None
        last_out_a = None
        last_out_global = -1
        entering_delay_target = None
        jumping_wait_idr = False
        catchup_target_delay = 0.0
        output_retries_used = 0

        def aac_frame_ms():
            sr = self.validator.sample_rate or 48000
            return math.ceil(1024 * 1000 / sr)

        def video_frame_ms():
            fps = self._stream_fmt.fps if self._stream_fmt else 30
            return math.ceil(1000 / max(1, fps))

        def align_to_frame_grid(min_ms, anchor_src_ms=0):
            fps = self._stream_fmt.fps if self._stream_fmt else 30
            step = Fraction(1000, max(1, fps))
            k = math.ceil(Fraction(min_ms - anchor_src_ms) / step)
            aligned = anchor_src_ms + int(round(k * step))
            if aligned < min_ms:
                aligned = anchor_src_ms + int(round((k + 1) * step))
            return aligned

        def safe_next_out_ts(anchor_src_ms=0):
            candidates = [last_out_global + 1]
            v_step = video_frame_ms()
            if last_out_v is not None:
                candidates.append(last_out_v + v_step)
            if last_out_v_pts is not None:
                candidates.append(last_out_v_pts + v_step)
            if last_out_a is not None:
                candidates.append(last_out_a + aac_frame_ms())
            return align_to_frame_grid(max(0, max(candidates)), anchor_src_ms)

        def emit_tag(target_stream, tag, out_ts):
            nonlocal last_out_v, last_out_v_pts, last_out_a, last_out_global
            data = tag.encode() if out_ts == tag.timestamp else tag.with_timestamp(out_ts).encode()
            self._write(target_stream, data)
            if tag.kind == 9:
                last_out_v = out_ts
                cts = 0
                if len(tag.payload) >= 5 and tag.payload[1] == 1:
                    cts = int.from_bytes(tag.payload[2:5], "big", signed=True)
                v_pts = out_ts + max(0, cts)
                last_out_v_pts = v_pts if last_out_v_pts is None else max(last_out_v_pts, v_pts)
            elif tag.kind == 8:
                last_out_a = out_ts
            last_out_global = max(last_out_global, out_ts)

        def emit_obs_headers(target_stream, out_ts, custom_headers=None):
            if custom_headers is not None:
                headers = [custom_headers[k] for k in (9, 8)]
            else:
                with self.lock:
                    headers = [self.validator.headers[k] for k in (9, 8)]
            for h in headers:
                if self.stop_event.is_set():
                    return False
                data = h.encode() if out_ts == h.timestamp else h.with_timestamp(out_ts).encode()
                self._write(target_stream, data)
            return True

        def open_output(initial_ts=0):
            nonlocal stream, audit
            if self.output_file:
                s = self.output_file.open("xb", buffering=0)
            else:
                self._spawn(self.output_command(), "output_proc")
                s = self.output_proc.stdin
            stream = s
            if self.stop_event.is_set():
                return False
            self._write(stream, FLV_HEADER)
            if not emit_obs_headers(stream, initial_ts):
                return False
            if self.audit_file and audit is None:
                audit = self.audit_file.open("x", encoding="utf-8")
            if self.set_state("RELAYING", {"BUFFERING"}):
                self.event("Envío al transporte iniciado.")
            return True

        def run_slate_loop(mode_name, *, stop_predicate, target_wait_s=0.0):
            """Generic continuous slate loop (used for delay fill, panic hold, purge resync, OBS reconnect)."""
            compiled, err = self._get_compiled_slate()
            if compiled is None:
                self.event(f"Slate no disponible ({err or 'sin formato'}).")
                return None

            slate_start_ms = safe_next_out_ts()
            self._write(stream, compiled.avc_header.with_timestamp(slate_start_ms).encode())
            self._write(stream, compiled.aac_header.with_timestamp(slate_start_ms).encode())

            cursor = SlateCursor(compiled, slate_start_ms, slate_start_ms)
            wall_start = time.perf_counter()
            with self.lock:
                self.delay_mode = mode_name
                self._slate_elapsed_s = 0.0
                self._slate_target_s = target_wait_s

            while not self.stop_event.is_set():
                now = time.perf_counter()
                elapsed = now - wall_start
                with self.lock:
                    self._slate_elapsed_s = min(elapsed, target_wait_s) if target_wait_s > 0 else elapsed
                limit_ms = stop_predicate(now, wall_start, slate_start_ms)
                if limit_ms is True:
                    break
                pkt_ts, pkt = cursor.next_packet(max_audio_end_ms=limit_ms if isinstance(limit_ms, int) else None)
                due_wall = wall_start + (pkt_ts - slate_start_ms) / 1000.0
                if self._wait_until(due_wall, interrupt_on_cmd=True):
                    return None
                if self.stop_event.is_set():
                    return None
                emit_tag(stream, pkt, pkt_ts)
                with self.lock:
                    self.slate_packets_sent += 1
                    self.last_progress = time.perf_counter()

            with self.lock:
                self._slate_elapsed_s = 0.0
                self._slate_target_s = 0.0
            return cursor

        def run_slate_bridge(anchor_idr_tag, new_delay_s):
            nonlocal ts_offset_ms
            release_wall = anchor_idr_tag.arrival + new_delay_s
            total_wait = max(0.1, release_wall - time.perf_counter())

            def should_stop(now, wall_start, slate_start_ms):
                nonlocal new_delay_s, release_wall, total_wait
                if now >= release_wall or self.ingress_done.is_set():
                    return True
                pending = self._peek_command()
                if pending and pending[0] in ("jump_live", "catchup_live", "panic_hold", "post_purge_resync"):
                    return True
                if pending and pending[0] == "enable_delay":
                    self._pop_command()
                    new_delay_s = pending[1]
                    release_wall = anchor_idr_tag.arrival + new_delay_s
                    total_wait = max(0.1, release_wall - wall_start)
                    with self.lock:
                        self._slate_target_s = total_wait
                return slate_start_ms + int((release_wall - wall_start) * 1000)

            cursor = run_slate_loop("slating", stop_predicate=should_stop, target_wait_s=total_wait)
            if self.stop_event.is_set():
                return
            if cursor is None:
                pacer.set_delay(new_delay_s)
                pacer.set_rate(1.0)
                with self.lock:
                    self.delay_mode = "delayed"
                return

            resume_out_ts = align_to_frame_grid(cursor.safe_resume_ts_ms(), anchor_idr_tag.timestamp)
            if not emit_obs_headers(stream, resume_out_ts):
                return
            ts_offset_ms = resume_out_ts - anchor_idr_tag.timestamp
            pacer.set_delay(new_delay_s)
            pacer.set_rate(1.0)
            pacer.rebase()
            with self.lock:
                if self.delay_mode == "slating":
                    self.delay_mode = "delayed"
            self.event(f"Delay de {new_delay_s:g} s activo. Reanudando señal real sin pérdida de frames.")

        def run_panic_hold():
            nonlocal ts_offset_ms, jumping_wait_idr
            def should_stop(now, wall_start, slate_start_ms):
                if self.ingress_done.is_set():
                    return True
                pending = self._peek_command()
                if pending and pending[0] in ("jump_live", "catchup_live", "panic_resume", "enable_delay"):
                    return True
                return None

            cursor = run_slate_loop("slate_hold", stop_predicate=should_stop, target_wait_s=0.0)
            if self.stop_event.is_set() or cursor is None:
                return
            pending = self._peek_command()
            if pending and pending[0] == "panic_resume":
                self._pop_command()
                nal_len = self.validator.nal_length
                # Align to an IDR in buffer (keep buffered content from first IDR onward)
                nxt = self.buffer.peek()
                while nxt is not None and not tag_has_idr(nxt, nal_len):
                    self.buffer.pop()
                    nxt = self.buffer.peek()
                if nxt is not None:
                    resume_out_ts = align_to_frame_grid(cursor.safe_resume_ts_ms(), nxt.timestamp)
                    if emit_obs_headers(stream, resume_out_ts):
                        ts_offset_ms = resume_out_ts - nxt.timestamp
                        pacer.set_delay(self.target_delay)
                        pacer.set_rate(1.0)
                        pacer.rebase()
                        with self.lock:
                            self.delay_mode = "delayed" if self.target_delay > 0 else "live"
                        self.event("Transmisión reanudada desde pantalla segura.")
                else:
                    jumping_wait_idr = True
                    with self.lock:
                        self.delay_mode = "jumping_live"

        def run_reconnect_input_slate():
            nonlocal ts_offset_ms, jumping_wait_idr
            def should_stop(now, wall_start, slate_start_ms):
                if self.ingress_done.is_set():
                    return True
                if not self._input_reconnecting and self.buffer.peek() is not None:
                    return True
                return None

            cursor = run_slate_loop("reconnecting_input", stop_predicate=should_stop,
                                    target_wait_s=self.config.reconnect_timeout)
            if self.stop_event.is_set() or cursor is None:
                return
            nxt = self.buffer.peek()
            if nxt is not None:
                resume_out_ts = align_to_frame_grid(cursor.safe_resume_ts_ms(), nxt.timestamp)
                with self.lock:
                    hdrs = self._epoch_boundaries.pop(nxt.timestamp, None)
                if emit_obs_headers(stream, resume_out_ts, custom_headers=hdrs):
                    ts_offset_ms = resume_out_ts - nxt.timestamp
                    pacer.set_delay(self.target_delay)
                    pacer.set_rate(1.0)
                    pacer.rebase()
                    with self.lock:
                        self.delay_mode = "delayed" if self.target_delay > 0 else "live"
            else:
                jumping_wait_idr = True

        def try_reconnect_output():
            nonlocal stream, output_retries_used, ts_offset_ms, last_out_v, last_out_v_pts, last_out_a, last_out_global
            if not self.config.auto_reconnect or self.output_file is not None:
                return False
            if output_retries_used >= self.config.output_retries:
                return False
            output_retries_used += 1
            with self.lock:
                self._output_reconnecting = True
            self.event(f"Destino RTMP caído. Reintentando conexión ({output_retries_used}/{self.config.output_retries})...")
            old_proc = self.output_proc
            self._close_stream(stream, fatal=False)
            stream = None
            self._terminate_proc(old_proc)
            if self.stop_event.wait(min(5.0, 1.0 * output_retries_used)):
                return False
            # Resync cleanly to newest IDR for fresh RTMP connection
            nal_len = self.validator.nal_length
            self.buffer.drop_until_latest_idr(lambda t: tag_has_idr(t, nal_len))
            nxt = self.buffer.peek()
            try:
                last_out_v = last_out_v_pts = last_out_a = None
                last_out_global = -1
                if not open_output(initial_ts=0):
                    return False
                ts_offset_ms = -nxt.timestamp if nxt is not None else 0
                pacer.rebase()
                with self.lock:
                    self._output_reconnecting = False
                self.output_reconnects += 1
                self.event(f"Salida RTMP reconectada (intento {output_retries_used}/{self.config.output_retries}).")
                return True
            except Exception:
                with self.lock:
                    self._output_reconnecting = False
                return False

        try:
            while not self.stop_event.is_set():
                # 1. Process live dynamic commands
                cmd = self._pop_command()
                if cmd is not None:
                    action, arg = cmd
                    if action == "enable_delay":
                        if stream is None:
                            pacer.set_delay(arg)
                            pacer.set_rate(1.0)
                            with self.lock:
                                self.delay_mode = "delayed"
                        else:
                            entering_delay_target = arg
                            jumping_wait_idr = False
                            pacer.set_rate(1.0)
                            with self.lock:
                                self.delay_mode = "entering_delay"
                    elif action == "panic_hold":
                        entering_delay_target = None
                        jumping_wait_idr = False
                        if stream is None and self.validator.ready:
                            if not open_output():
                                break
                        if stream is not None:
                            run_panic_hold()
                            continue
                    elif action == "post_purge_resync":
                        entering_delay_target = None
                        if stream is not None:
                            def stop_when_fresh_idr(now, wall_start, slate_start_ms):
                                if self.ingress_done.is_set():
                                    return True
                                nxt = self.buffer.peek()
                                if nxt is not None and tag_has_idr(nxt, self.validator.nal_length):
                                    return True
                                return None
                            cursor = run_slate_loop("slating", stop_predicate=stop_when_fresh_idr, target_wait_s=2.0)
                            nxt = self.buffer.peek()
                            if cursor is not None and nxt is not None and not self.stop_event.is_set():
                                resume_out_ts = align_to_frame_grid(cursor.safe_resume_ts_ms(), nxt.timestamp)
                                if emit_obs_headers(stream, resume_out_ts):
                                    ts_offset_ms = resume_out_ts - nxt.timestamp
                                    pacer.rebase()
                                    with self.lock:
                                        self.delay_mode = "delayed" if self.target_delay > 0 else "live"
                            else:
                                jumping_wait_idr = True
                            continue
                    elif action == "jump_live":
                        entering_delay_target = None
                        nal_len = self.validator.nal_length
                        dropped, target_idr = self.buffer.drop_until_latest_idr(
                            lambda t: tag_has_idr(t, nal_len)
                        )
                        pacer.set_delay(0.0)
                        pacer.set_rate(1.0)
                        pacer.rebase()
                        if target_idr is not None:
                            if stream is not None:
                                resume_out_ts = safe_next_out_ts(target_idr.timestamp)
                                if not emit_obs_headers(stream, resume_out_ts):
                                    break
                                ts_offset_ms = resume_out_ts - target_idr.timestamp
                            jumping_wait_idr = False
                            with self.lock:
                                self.dropped_jump_packets += dropped
                                self.delay_mode = "live"
                            self.event(f"En directo (0 s). Salto limpio al último IDR ({dropped} paquetes omitidos).")
                        else:
                            jumping_wait_idr = True
                            with self.lock:
                                self.delay_mode = "jumping_live"
                    elif action == "jump_to_delay":
                        entering_delay_target = None
                        nal_len = self.validator.nal_length
                        dropped, target_idr = self.buffer.drop_to_target_delay(
                            arg, time.perf_counter(), lambda t: tag_has_idr(t, nal_len)
                        )
                        pacer.set_delay(arg)
                        pacer.set_rate(1.0)
                        pacer.rebase()
                        if target_idr is not None and stream is not None:
                            resume_out_ts = safe_next_out_ts(target_idr.timestamp)
                            if not emit_obs_headers(stream, resume_out_ts):
                                break
                            ts_offset_ms = resume_out_ts - target_idr.timestamp
                            with self.lock:
                                self.dropped_jump_packets += dropped
                        with self.lock:
                            self.delay_mode = "delayed"
                        self.event(f"Delay ajustado a {arg:g} s ({dropped} paquetes omitidos).")
                    elif action == "catchup_live":
                        entering_delay_target = None
                        jumping_wait_idr = False
                        catchup_target_delay = 0.0
                        pacer.set_delay(0.0)
                        pacer.set_rate(arg)
                        with self.lock:
                            self.delay_mode = "catching_up"
                    elif action == "catchup_to_delay":
                        target_d, rate = arg
                        entering_delay_target = None
                        jumping_wait_idr = False
                        catchup_target_delay = target_d
                        pacer.set_delay(target_d)
                        pacer.set_rate(rate)
                        with self.lock:
                            self.delay_mode = "catching_up"

                # 2. Peek next buffered tag (or hold slate if OBS is reconnecting!)
                tag = self.buffer.peek()
                if tag is None:
                    if self._input_reconnecting and stream is not None:
                        run_reconnect_input_slate()
                        continue
                    if self.ingress_done.is_set():
                        break
                    self._cmd_event.wait(0.01)
                    continue
                with self.lock:
                    ready = self.validator.ready
                if not ready:
                    if self.ingress_done.is_set():
                        raise RuntimeError("Entrada incompleta.")
                    self.stop_event.wait(0.01)
                    continue

                # 3. Handle new input epoch headers or post-purge clean IDR splice
                with self.lock:
                    epoch_hdrs = self._epoch_boundaries.pop(tag.timestamp, None)
                    is_purge_splice = tag.timestamp in self._splice_idrs
                    if is_purge_splice:
                        self._splice_idrs.discard(tag.timestamp)
                if (epoch_hdrs is not None or is_purge_splice) and stream is not None:
                    resume_out_ts = safe_next_out_ts(tag.timestamp)
                    if not emit_obs_headers(stream, resume_out_ts, custom_headers=epoch_hdrs):
                        break
                    ts_offset_ms = resume_out_ts - tag.timestamp
                    pacer.rebase()

                # 4. Handle pending IDR jump if buffer had no IDR at the moment of click
                if jumping_wait_idr:
                    if tag_has_idr(tag, self.validator.nal_length):
                        if stream is not None:
                            resume_out_ts = safe_next_out_ts(tag.timestamp)
                            if not emit_obs_headers(stream, resume_out_ts):
                                break
                            ts_offset_ms = resume_out_ts - tag.timestamp
                        pacer.rebase()
                        jumping_wait_idr = False
                        with self.lock:
                            self.delay_mode = "delayed" if self.target_delay > 0 else "live"
                        self.event("Sincronizado en nuevo keyframe IDR.")
                    else:
                        self.buffer.pop()
                        with self.lock:
                            self.dropped_jump_packets += 1
                        continue

                # 5. Handle pending live delay activation at clean IDR boundary (0 lost frames)
                if entering_delay_target is not None and stream is not None:
                    if tag_has_idr(tag, self.validator.nal_length):
                        new_delay = entering_delay_target
                        entering_delay_target = None
                        run_slate_bridge(tag, new_delay)
                        continue

                # 6. Pace delivery (interruptible if user changes delay mode mid-wait!)
                if self._wait_until(pacer.deadline(tag), interrupt_on_cmd=True):
                    break
                if self._peek_command() is not None:
                    continue

                if stream is None:
                    if not open_output():
                        break
                if self.stop_event.is_set():
                    break

                # 7. Emit OBS packet with continuous monotonic output timestamp (with optional output retry)
                out_ts = max(0, tag.timestamp + ts_offset_ms)
                try:
                    emit_tag(stream, tag, out_ts)
                except (BrokenPipeError, OSError):
                    if try_reconnect_output():
                        continue
                    raise
                now = time.perf_counter()
                pacer.sent(tag, now)
                self.buffer.pop()
                output_retries_used = 0

                residence = now - tag.arrival
                with self.lock:
                    self.sent += 1
                    self.last_age = residence
                    self.last_progress = now
                    self.last_sent_ts = out_ts
                    if self.delay_mode == "catching_up":
                        buf_span = self.buffer.snapshot()["seconds"]
                        if catchup_target_delay > 0:
                            if residence <= catchup_target_delay + 0.20:
                                pacer.set_rate(1.0)
                                self.delay_mode = "delayed"
                                self.event(f"Delay estabilizado en {catchup_target_delay:g} s sin perder frames.")
                        elif residence <= 0.20 and buf_span <= 0.25:
                            pacer.set_rate(1.0)
                            self.delay_mode = "live"
                            self.event("¡En directo (0 s)! Alcanzado sin perder ningún frame.")
                if audit and self.audit_count < 500000:
                    audit.write(json.dumps({"kind": tag.kind, "ts_ms": out_ts,
                                            "src_ts_ms": tag.timestamp,
                                            "arrival": tag.arrival, "sent": now,
                                            "residence_s": residence}) + "\n")
                    self.audit_count += 1

            if stream:
                stream.close(); stream = None
            if self.output_proc and not self.stop_event.is_set():
                if self.output_proc.wait(timeout=self.config.stall_timeout + 2) != 0:
                    raise RuntimeError("El destino RTMP rechazó o interrumpió la salida.")
            if not self.stop_event.is_set():
                completed = True
        except Exception as exc:
            if not self.stop_event.is_set():
                if isinstance(exc, FileExistsError):
                    self.fail("El archivo de prueba ya existe; no se sobrescribe.")
                else:
                    self.fail("No se pudo entregar la salida. Sesión detenida; revisa el destino.")
        finally:
            try:
                self._close_stream(stream)
                self._close_stream(audit)
            finally:
                self.buffer.discard()
                if completed and self.set_state("FINISHED"):
                    self.event("Contenido pendiente enviado. Sesión finalizada.")
                self.done.set()

    def _watchdog(self):
        while not self.done.wait(.25):
            if self.stop_event.is_set():
                return
            now = time.perf_counter()
            with self.lock:
                writing = self.write_started
                last_input = self.last_input
                in_reconn = self._input_reconnecting
                in_reconn_since = self._input_reconnect_since
                out_reconn = self._output_reconnecting
            if in_reconn and in_reconn_since is not None and now - in_reconn_since >= self.config.reconnect_timeout:
                with self.lock:
                    self._input_reconnect_timed_out = True
                    proc = self.input_proc
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            if writing is not None and now - writing > self.config.stall_timeout:
                if not out_reconn:
                    self.fail("El transporte de salida se bloqueó. Sesión detenida.")
                    return
            if self.output_proc and self.output_proc.poll() is not None:
                if not self.config.auto_reconnect and not out_reconn:
                    if not self.ingress_done.is_set() or self.output_proc.returncode != 0:
                        self.fail("El destino RTMP se desconectó. Reinicia la sesión para reconectar.")
                        return
            if last_input and not self.ingress_done.is_set() and not in_reconn and now - last_input > self.config.stall_timeout:
                if not self.config.auto_reconnect:
                    self.fail("La fuente dejó de entregar datos. Sesión detenida.")
                    return
            self.snapshot()

    def fail(self, reason, *, during_stop=False):
        with self.lock:
            if (self.state in {"FAULT", "STOPPED", "FINISHED"}
                    or (self._stop_requested and not during_stop)):
                return False
            self.state, self.error = "FAULT", reason
            self.event(reason)
            self.stop_event.set()
            self._cmd_event.set()
        self.buffer.close()
        self._terminate(force=True)
        with self.lifecycle_lock:
            if self.egress_thread is None or not self.egress_thread.is_alive():
                self.buffer.discard()
                self.done.set()
        return True

    def _terminate(self, force=False):
        with self.lifecycle_lock:
            for proc in (self.input_proc, self.output_proc):
                if proc and proc.poll() is None:
                    try:
                        proc.kill() if force else proc.terminate()
                    except ProcessLookupError:
                        pass
                    except OSError:
                        with self.lock:
                            self.transport_diagnostics.append("No se pudo interrumpir un transporte; cierre pendiente.")

    def stop(self, timeout=2.0):
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Timeout de cierre inválido.")
        with self.lock:
            self._stop_requested = True
            self.stop_event.set()
            self._cmd_event.set()
        self.buffer.close()
        with self.lifecycle_lock:
            processes = tuple(p for p in (self.input_proc, self.output_proc) if p)
            threads = tuple(self.threads)
        self._terminate()
        deadline = time.perf_counter() + timeout
        for proc in processes:
            try:
                proc.wait(timeout=max(0, deadline - time.perf_counter()))
            except (subprocess.TimeoutExpired, OSError):
                self._terminate(force=True)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.perf_counter()))
        pending = any(t.is_alive() for t in threads) or any(p.poll() is None for p in processes)
        if pending:
            self.fail("El cierre sigue pendiente. Espera antes de preparar otra sesión.", during_stop=True)
            return False
        for proc in processes:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                self._close_stream(pipe)
        self.buffer.discard()
        self.done.set()
        with self.lock:
            if self.state not in {"FAULT", "FINISHED"}:
                self.state = "STOPPED"
                self.event("Sesión detenida por el usuario. El contenido pendiente no se envía.")
        return True

    def snapshot(self):
        with self.lock:
            pids = [os.getpid()] + [p.pid for p in (self.input_proc, self.output_proc)
                                    if p and p.poll() is None]
            stats = process_stats(pids)
            self.rss_peak = max(self.rss_peak, stats["rss_bytes"] or 0)
            elapsed = time.perf_counter() - self.started if self.started else 0
            age = self.last_age
            buf = self.buffer.snapshot()
            catchup_eta = None
            if self.delay_mode == "catching_up" and self.catchup_rate > 1.0:
                rem = max(0.0, (age if age is not None else buf["seconds"]) - self.target_delay)
                catchup_eta = rem / (self.catchup_rate - 1.0)
            slate_progress = (
                min(1.0, self._slate_elapsed_s / self._slate_target_s)
                if self._slate_target_s > 0 else 0.0
            )
            return {
                "state": self.state,
                "delay_mode": self.delay_mode,
                "error": self.error,
                "delay_target_s": self.target_delay,
                "default_delay_s": self.config.default_delay,
                "catchup_rate": self.catchup_rate,
                "catchup_eta_s": catchup_eta,
                "last_packet_residence_s": age,
                "received_packets": self.received,
                "sent_packets": self.sent,
                "slate_packets_sent": self.slate_packets_sent,
                "dropped_jump_packets": self.dropped_jump_packets,
                "purged_packets": self.purged_packets,
                "last_purge": self.last_purge,
                "panic_active": self._panic_active or self.delay_mode == "slate_hold",
                "auto_reconnect": self.config.auto_reconnect,
                "reconnects": {
                    "input": self.input_reconnects,
                    "output": self.output_reconnects,
                    "input_reconnecting": self._input_reconnecting,
                    "output_reconnecting": self._output_reconnecting,
                },
                "elapsed_s": elapsed,
                "buffer": buf,
                "memory": {**stats, "rss_peak_bytes": self.rss_peak or None},
                "input_url": self.config.input_url,
                "output_url": mask_rtmp_url(self.config.output_url),
                "preset": self.config.preset,
                "events": list(self.events),
                "sample_rate": self.validator.sample_rate,
                "channels": self.validator.channels,
                "stream": {
                    "width": self._stream_fmt.width if self._stream_fmt else None,
                    "height": self._stream_fmt.height if self._stream_fmt else None,
                    "fps": self._stream_fmt.fps if self._stream_fmt else None,
                    "sample_rate": self.validator.sample_rate,
                    "channels": self.validator.channels,
                    "has_bframes": self.validator.has_bframes,
                },
                "slate": {
                    "ready": self._compiled_slate is not None,
                    "compiling": self._slate_compiling,
                    "is_custom": self._compiled_slate.is_custom_image if self._compiled_slate else False,
                    "elapsed_s": self._slate_elapsed_s,
                    "target_s": self._slate_target_s,
                    "progress": slate_progress,
                    "error": self._slate_error,
                },
                "shutdown_complete": (
                    self.done.is_set()
                    and not any(t.is_alive() for t in self.threads)
                    and all(p.poll() is not None for p in (self.input_proc, self.output_proc) if p)
                ),
                "capabilities": {
                    "fixed_delay": True,
                    "dynamic_delay": True,
                    "slate": True,
                    "panic_slate": True,
                    "catchup": True,
                    "jump_live": True,
                    "purge": True,
                    "auto_reconnect": self.config.auto_reconnect,
                    "bframes_compatible": self.config.allow_bframes,
                    "remote_rtmp": self.config.allow_remote_output,
                },
                "version": __version__,
            }
