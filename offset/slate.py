"""Custom image slate storage, ingest-matched FLV compilation, and exact A/V loop cursor."""
from dataclasses import dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time

from .media import FLV_HEADER, Tag, read_tags
from .paths import data_root, resolve_binary

MAX_SLATE_IMAGE_BYTES = 10 * 1024 * 1024
COMMON_FPS = (24, 25, 30, 48, 50, 60)
SAFE_COLORSPACES = {"bt709", "bt470bg", "smpte170m"}
SAFE_PRIMARIES = {"bt709", "bt470bg", "smpte170m"}
SAFE_TRANSFERS = {"bt709", "smpte170m", "iec61966-2-1"}
SAFE_RANGES = {"tv", "pc"}


def detect_image_extension(data):
    """Validate image magic bytes and return safe extension or raise ValueError."""
    if not data or len(data) > MAX_SLATE_IMAGE_BYTES:
        raise ValueError("La imagen debe pesar menos de 10 MiB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("Formato no admitido. Sube una imagen PNG, JPG o WebP.")


def mime_for_extension(ext):
    return {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")


class SlateStore:
    """Persists the streamer's custom slate image locally."""
    def __init__(self, directory=None):
        root = Path(directory) if directory else data_root() / "slate"
        self.directory = root
        self.meta_path = root / "slate.json"

    def _existing_image(self):
        if not self.directory.exists():
            return None
        for ext in (".png", ".jpg", ".webp"):
            candidate = self.directory / f"custom_slate{ext}"
            if candidate.is_file():
                return candidate
        return None

    def save_image(self, data, original_name="slate"):
        ext = detect_image_extension(data)
        self.directory.mkdir(parents=True, exist_ok=True)
        for old_ext in (".png", ".jpg", ".webp"):
            old = self.directory / f"custom_slate{old_ext}"
            if old.exists():
                old.unlink()
        dest = self.directory / f"custom_slate{ext}"
        dest.write_bytes(data)
        safe_name = Path(str(original_name or "slate")).name[:80]
        meta = {
            "filename": safe_name,
            "ext": ext,
            "size_bytes": len(data),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return self.info()

    def delete_image(self):
        img = self._existing_image()
        if img and img.exists():
            img.unlink()
        if self.meta_path.exists():
            self.meta_path.unlink()
        return self.info()

    def get_image_path(self):
        return self._existing_image()

    def read_image(self):
        path = self._existing_image()
        if not path:
            return None, None
        return path.read_bytes(), mime_for_extension(path.suffix.lower())

    def info(self):
        path = self._existing_image()
        if not path:
            return {"has_custom": False, "filename": None, "size_bytes": 0, "updated_at": None}
        meta = {}
        if self.meta_path.is_file():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        return {
            "has_custom": True,
            "filename": meta.get("filename") or path.name,
            "size_bytes": path.stat().st_size,
            "updated_at": meta.get("updated_at"),
        }


@dataclass(frozen=True)
class StreamFormat:
    width: int
    height: int
    fps: int
    sample_rate: int
    channels: int
    profile: str = "high"
    level: str = "4.1"
    color_space: str = "bt709"
    color_primaries: str = "bt709"
    color_transfer: str = "bt709"
    color_range: str = "tv"


def _format_level(raw_level, width, height, fps):
    try:
        num = int(raw_level)
        if 30 <= num <= 52:
            return f"{num // 10}.{num % 10}"
    except (TypeError, ValueError):
        pass
    if width >= 1920 and fps >= 50:
        return "4.2"
    if width >= 1920:
        return "4.1"
    return "3.1"


def estimate_fps(video_dts_samples, fallback=30):
    if len(video_dts_samples) >= 4:
        deltas = [b - a for a, b in zip(video_dts_samples, video_dts_samples[1:]) if 5 <= (b - a) <= 100]
        if deltas:
            med = statistics.median(deltas)
            raw = 1000.0 / med
            for common in COMMON_FPS:
                if abs(raw - common) / common <= 0.04:
                    return common
            return max(15, min(60, int(round(raw))))
    return fallback


def probe_stream_format(avc_header, aac_header, first_idr, sample_rate, channels,
                        video_dts_samples=(), ffmpeg="ffmpeg"):
    """Inspect width/height/profile/level/colorimetry from live OBS stream headers + first IDR."""
    ffprobe = resolve_binary("ffprobe")
    if ffprobe == "ffprobe" and ffmpeg != "ffmpeg":
        candidate = Path(ffmpeg).with_name("ffprobe.exe" if Path(ffmpeg).suffix.lower() == ".exe" else "ffprobe")
        if candidate.is_file():
            ffprobe = str(candidate)

    width, height, profile = 1280, 720, "high"
    raw_level = None
    color_space, color_primaries, color_transfer, color_range = "bt709", "bt709", "bt709", "tv"
    fps = estimate_fps(video_dts_samples, fallback=30)

    if ffprobe and avc_header and aac_header and first_idr:
        try:
            with tempfile.TemporaryDirectory(prefix="offset-probe-") as tmp:
                sample = Path(tmp) / "sample.flv"
                raw = (FLV_HEADER + avc_header.with_timestamp(0).encode()
                       + aac_header.with_timestamp(0).encode()
                       + first_idr.with_timestamp(0).encode())
                sample.write_bytes(raw)
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                proc = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries",
                     "stream=width,height,profile,level,r_frame_rate,color_space,color_primaries,color_transfer,color_range",
                     "-of", "json", str(sample)],
                    capture_output=True, timeout=5, creationflags=flags,
                )
                if proc.returncode == 0:
                    data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
                    streams = data.get("streams") or []
                    if streams:
                        s = streams[0]
                        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
                        if w >= 64 and h >= 64:
                            width, height = w - (w % 2), h - (h % 2)
                        prof = str(s.get("profile") or "").lower()
                        if "baseline" in prof:
                            profile = "baseline"
                        elif "main" in prof:
                            profile = "main"
                        else:
                            profile = "high"
                        raw_level = s.get("level")
                        cs = str(s.get("color_space") or "").lower()
                        cp = str(s.get("color_primaries") or "").lower()
                        ct = str(s.get("color_transfer") or "").lower()
                        cr = str(s.get("color_range") or "").lower()
                        if cs in SAFE_COLORSPACES:
                            color_space = cs
                        if cp in SAFE_PRIMARIES:
                            color_primaries = cp
                        if ct in SAFE_TRANSFERS:
                            color_transfer = ct
                        if cr in SAFE_RANGES:
                            color_range = cr
                        if len(video_dts_samples) < 4 and "/" in str(s.get("r_frame_rate", "")):
                            num, den = str(s["r_frame_rate"]).split("/", 1)
                            if int(den) > 0 and 15 <= int(num) / int(den) <= 60:
                                fps = int(round(int(num) / int(den)))
        except Exception:
            pass
    return StreamFormat(
        width=width,
        height=height,
        fps=fps,
        sample_rate=sample_rate or 48000,
        channels=channels or 2,
        profile=profile,
        level=_format_level(raw_level, width, height, fps),
        color_space=color_space,
        color_primaries=color_primaries,
        color_transfer=color_transfer,
        color_range=color_range,
    )


@dataclass(frozen=True)
class CompiledSlate:
    fmt: StreamFormat
    avc_header: Tag
    aac_header: Tag
    video_tags: tuple
    audio_tags: tuple
    is_custom_image: bool

    @property
    def video_step_ms(self):
        return Fraction(1000, self.fmt.fps)

    @property
    def audio_step_ms(self):
        return Fraction(1024 * 1000, self.fmt.sample_rate)


def compile_slate(fmt, image_path=None, ffmpeg="ffmpeg", duration_s=2):
    """Compile custom image (or dark default slate) matching OBS H.264 level, colorimetry & AUD."""
    w, h, fps = fmt.width, fmt.height, fmt.fps
    sr, ch = fmt.sample_rate, fmt.channels
    cl = "stereo" if ch == 2 else "mono"
    gop = max(fps, int(fps * duration_s))
    use_custom = image_path is not None and Path(image_path).is_file()
    ff = resolve_binary(ffmpeg)

    with tempfile.TemporaryDirectory(prefix="offset-slate-") as tmp:
        out_flv = Path(tmp) / "slate.flv"
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if use_custom:
            cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(image_path)]
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=#0a0a0c,setsar=1,format=yuv420p")
        else:
            cmd += ["-f", "lavfi", "-i", f"color=c=#0a0a0c:s={w}x{h}:r={fps}"]
            vf = "format=yuv420p"
        cmd += [
            "-f", "lavfi", "-i", f"anullsrc=r={sr}:cl={cl}",
            "-t", str(duration_s),
            "-vf", vf,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-profile:v", fmt.profile, "-level:v", fmt.level, "-pix_fmt", "yuv420p",
            "-colorspace", fmt.color_space, "-color_primaries", fmt.color_primaries,
            "-color_trc", fmt.color_transfer, "-color_range", fmt.color_range,
            "-x264-params", "aud=1:repeat-headers=1",
            "-bf", "0", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-b:v", "800k", "-maxrate", "1500k", "-bufsize", "1500k",
            "-c:a", "aac", "-b:a", "128k", "-ar", str(sr), "-ac", str(ch),
            "-flvflags", "no_duration_filesize",
            "-f", "flv", str(out_flv),
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run(cmd, capture_output=True, timeout=20, creationflags=flags)
        if proc.returncode != 0:
            raise RuntimeError("No se pudo generar el slate compatible con FFmpeg: "
                               + proc.stderr.decode("utf-8", errors="replace")[-300:])
        avc_header = aac_header = None
        video_tags, audio_tags = [], []
        with out_flv.open("rb") as stream:
            for tag in read_tags(stream):
                if tag.kind == 9:
                    if len(tag.payload) >= 2 and tag.payload[1] == 0:
                        avc_header = tag.with_timestamp(0)
                    elif len(tag.payload) >= 2 and tag.payload[1] == 1:
                        video_tags.append(tag)
                elif tag.kind == 8:
                    if len(tag.payload) >= 2 and tag.payload[1] == 0:
                        aac_header = tag.with_timestamp(0)
                    elif len(tag.payload) >= 2 and tag.payload[1] == 1:
                        audio_tags.append(tag)
        if not (avc_header and aac_header and video_tags and audio_tags):
            raise RuntimeError("El slate compilado quedó incompleto.")
        return CompiledSlate(
            fmt=fmt,
            avc_header=avc_header,
            aac_header=aac_header,
            video_tags=tuple(video_tags),
            audio_tags=tuple(audio_tags),
            is_custom_image=use_custom,
        )


class SlateCursor:
    """Exact-Fraction scheduler for looping a compiled slate without AAC overlap."""
    def __init__(self, slate, start_video_ms, start_audio_ms):
        self.slate = slate
        self.v_step = slate.video_step_ms
        self.a_step = slate.audio_step_ms
        self.v_exact = Fraction(start_video_ms)
        self.a_exact = Fraction(start_audio_ms)
        self.v_idx = 0
        self.a_idx = 0
        self.last_video_exact = None
        self.last_audio_exact = None
        self.last_emitted_ms = max(int(start_video_ms), int(start_audio_ms)) - 1

    def peek_next_ms(self):
        return int(round(min(self.v_exact, self.a_exact)))

    def next_packet(self, *, max_audio_end_ms=None):
        """Return (out_ts_ms, Tag) in strictly non-decreasing FLV timestamp order."""
        v_due = self.v_exact
        a_due = self.a_exact
        audio_allowed = max_audio_end_ms is None or (a_due + self.a_step <= Fraction(max_audio_end_ms))

        if audio_allowed and a_due <= v_due:
            ts = max(int(round(a_due)), self.last_emitted_ms)
            base = self.slate.audio_tags[self.a_idx % len(self.slate.audio_tags)]
            self.a_idx += 1
            self.last_audio_exact = a_due
            self.a_exact = a_due + self.a_step
            self.last_emitted_ms = ts
            return ts, base.with_timestamp(ts)

        ts = max(int(round(v_due)), self.last_emitted_ms)
        base = self.slate.video_tags[self.v_idx % len(self.slate.video_tags)]
        self.v_idx += 1
        self.last_video_exact = v_due
        self.v_exact = v_due + self.v_step
        self.last_emitted_ms = ts
        return ts, base.with_timestamp(ts)

    def safe_resume_ts_ms(self):
        """Earliest integer ms safe to start the next real OBS segment without overlap."""
        candidates = [Fraction(self.last_emitted_ms + 1)]
        if self.last_video_exact is not None:
            candidates.append(self.last_video_exact + self.v_step)
        if self.last_audio_exact is not None:
            candidates.append(self.last_audio_exact + self.a_step)
        return math.ceil(max(candidates))
