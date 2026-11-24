"""Unit tests for dynamic delay, B-frame compatibility, slate, presets, hotkeys, and API."""
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from offset.buffer import PacketBuffer, Pacer
from offset.destinations import build_output_url, mask_rtmp_url, validate_output_rtmp
from offset.hotkeys import parse_hotkey
from offset.icon import build_ico_bytes
from offset.media import Tag, Validator, tag_has_idr
from offset.paths import resolve_binary
from offset.relay import Config, process_stats
from offset.server import make_server
from offset.settings import SettingsStore
from offset.slate import CompiledSlate, SlateCursor, SlateStore, StreamFormat, detect_image_extension
from tests.test_core import video, video_header

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class DynamicCoreTests(unittest.TestCase):
    def test_bframes_allowed_when_enabled(self):
        v = Validator(allow_bframes=True)
        v.accept(video_header())
        v.accept(Tag(8, 0, b"\xaf\x00\x11\x90", 0))
        self.assertTrue(v.accept(video(ts=0, cts=33)))
        # B-slice NAL (slice_type=1) with non-zero CTS
        self.assertTrue(v.accept(video(b"\x41\xa0", ts=33, cts=66)))
        self.assertTrue(v.has_bframes)
        self.assertFalse(v.last_is_idr)

    def test_drop_until_latest_idr_keeps_newest_idr_and_aligned_audio(self):
        buf = PacketBuffer(1024 * 1024, 60)
        idr1 = video(b"\x65\xb0", ts=0)
        p1 = video(b"\x41\xe0", ts=33)
        a1 = Tag(8, 21, b"\xaf\x01\x00", 0.021)
        idr2 = video(b"\x65\xb0", ts=2000)
        a2 = Tag(8, 2000, b"\xaf\x01\x01", 2.000)
        p2 = video(b"\x41\xe0", ts=2033)
        for t in (idr1, a1, p1, idr2, a2, p2):
            buf.put(t)
        dropped, found = buf.drop_until_latest_idr(lambda t: tag_has_idr(t, 4))
        self.assertEqual(dropped, 3)
        self.assertEqual(found, idr2)
        self.assertEqual(buf.snapshot()["packets"], 3)
        self.assertEqual(buf.pop(), idr2)
        self.assertEqual(buf.pop(), a2)
        self.assertEqual(buf.pop(), p2)
        self.assertEqual(buf.bytes, 0)

    def test_pacer_catchup_rate_and_jitter_absorption(self):
        p = Pacer(delay=0.0, rate=1.25)
        t0 = Tag(9, 0, b"", 0.0)
        p.sent(t0, 10.0)
        t1 = Tag(9, 1000, b"", 1.0)
        # 1000 ms media at 1.25x = 0.8 s wall time
        self.assertAlmostEqual(p.deadline(t1), 10.8, places=6)

        # Windows 20 ms scheduler jitter does not drift upward
        p2 = Pacer(delay=2.0)
        for n in range(200):
            t = Tag(9, n * 33, b"", n * 0.033)
            p2.sent(t, p2.deadline(t) + 0.020)
        self.assertAlmostEqual(p2.last_sent, 2.0 + 199 * 0.033, places=4)

    def test_slate_cursor_never_overlaps_aac_14ms_regression(self):
        # SPEC §6.1 mandatory regression: 48 kHz AAC frame is 21.333 ms;
        # a 14 ms gap must NOT insert an overlapping AAC frame.
        fmt = StreamFormat(640, 360, 30, 48000, 2)
        slate = CompiledSlate(
            fmt=fmt,
            avc_header=video_header(),
            aac_header=Tag(8, 0, b"\xaf\x00\x11\x90", 0),
            video_tags=(video(ts=0), video(b"\x41\xe0", ts=33)),
            audio_tags=(Tag(8, 0, b"\xaf\x01\xaa", 0),),
            is_custom_image=False,
        )
        cursor = SlateCursor(slate, start_video_ms=1000, start_audio_ms=1000)
        # With limit 1014 ms (14 ms gap), 1000 + 21.333 > 1014 -> audio MUST be skipped
        ts, pkt = cursor.next_packet(max_audio_end_ms=1014)
        self.assertEqual(pkt.kind, 9)
        self.assertEqual(ts, 1000)
        # With limit 1022 ms, audio fits cleanly without overlap
        ts2, pkt2 = cursor.next_packet(max_audio_end_ms=1022)
        self.assertEqual(pkt2.kind, 8)
        self.assertEqual(ts2, 1000)
        self.assertGreaterEqual(cursor.safe_resume_ts_ms(), 1022)

    def test_slate_store_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlateStore(tmp)
            self.assertFalse(store.info()["has_custom"])
            with self.assertRaises(ValueError):
                detect_image_extension(b"not an image")
            info = store.save_image(TINY_PNG, "mi_brb.png")
            self.assertTrue(info["has_custom"])
            self.assertEqual(info["filename"], "mi_brb.png")
            raw, mime = store.read_image()
            self.assertEqual(raw, TINY_PNG)
            self.assertEqual(mime, "image/png")
            store.delete_image()
            self.assertFalse(store.info()["has_custom"])

    def test_destinations_and_secret_masking(self):
        tw = build_output_url("twitch", stream_key="live_999999999_secretkey123")
        self.assertTrue(tw.startswith("rtmps://"))
        masked = mask_rtmp_url(tw)
        self.assertNotIn("secretkey123", masked)
        self.assertIn("••••", masked)

        yt = build_output_url("youtube", stream_key="abcd-efgh-ijkl-mnop")
        self.assertIn("youtube.com", yt)
        self.assertNotIn("efgh-ijkl", mask_rtmp_url(yt))

        with self.assertRaises(ValueError):
            validate_output_rtmp("file:///etc/passwd", allow_remote=True)
        with self.assertRaises(ValueError):
            build_output_url("twitch", stream_key="")

    def test_hotkey_parser(self):
        mods, vk = parse_hotkey("Ctrl+Shift+F9")
        self.assertTrue(mods & 0x0002)
        self.assertTrue(mods & 0x0004)
        self.assertEqual(vk, 0x78)
        with self.assertRaises(ValueError):
            parse_hotkey("Ctrl+Shift")
        with self.assertRaises(ValueError):
            parse_hotkey("Ctrl+INVALIDKEY")

    def test_windows_native_process_stats(self):
        if os.name == "nt":
            stats = process_stats([os.getpid()])
            self.assertIsNotNone(stats["rss_bytes"])
            self.assertGreater(stats["rss_bytes"], 1024 * 1024)
            self.assertIsNotNone(stats["cpu_seconds"])

    def test_http_slate_upload_dock_and_dynamic_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(Config(delay=0), 0, slate_store=SlateStore(tmp))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            root = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(root + "/?dock=1") as resp:
                    html = resp.read().decode()
                    self.assertEqual(resp.status, 200)
                token = re.search(r'name="offset-token" content="([^"]+)"', html)[1]

                # Upload custom PNG slate
                req = Request(
                    root + "/api/slate",
                    data=TINY_PNG,
                    headers={
                        "X-Offset-Token": token,
                        "Content-Type": "image/png",
                        "X-Slate-Filename": "espera.png",
                    },
                )
                with urlopen(req) as resp:
                    body = json.load(resp)
                    self.assertTrue(body["slate_store"]["has_custom"])

                # Preview image served cleanly
                with urlopen(root + "/slate-preview") as resp:
                    self.assertEqual(resp.read(), TINY_PNG)

                # Reset slate
                req_reset = Request(
                    root + "/api/slate/reset",
                    data=b"{}",
                    headers={"X-Offset-Token": token, "Content-Type": "application/json"},
                )
                with urlopen(req_reset) as resp:
                    self.assertFalse(json.load(resp)["slate_store"]["has_custom"])

                # Update default delay while READY
                req_delay = Request(
                    root + "/api/delay/enable",
                    data=json.dumps({"delay": 45}).encode(),
                    headers={"X-Offset-Token": token, "Content-Type": "application/json"},
                )
                with urlopen(req_delay) as resp:
                    self.assertEqual(json.load(resp)["default_delay_s"], 45.0)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_buffer_purge_recent_and_partial_delay_jump(self):
        buf = PacketBuffer(1024 * 1024, 60)
        # 3 GOPs at ts=0, 2000, 4000 ms (arrival=0, 2, 4 s)
        tags = [
            video(b"\x65\xb0", ts=0), Tag(8, 21, b"\xaf\x01\x00", 0.021), video(b"\x41\xe0", ts=33),
            video(b"\x65\xb0", ts=2000), Tag(8, 2021, b"\xaf\x01\x01", 2.021), video(b"\x41\xe0", ts=2033),
            video(b"\x65\xb0", ts=4000), Tag(8, 4021, b"\xaf\x01\x02", 4.021), video(b"\x41\xe0", ts=4033),
        ]
        for t in tags:
            buf.put(t)
        # Purge newest 1.5 s -> should excise GOP at 4000 ms (and/or 2000 ms depending on cutoff 2533)
        purged, span, emptied = buf.purge_recent(1.5, lambda t: tag_has_idr(t, 4))
        self.assertFalse(emptied)
        self.assertGreaterEqual(purged, 3)
        self.assertGreater(span, 0.0)
        # Only clean older GOP(s) remain
        self.assertEqual(buf.peek().timestamp, 0)

        # Partial jump: target delay 2.5 s at now=5.0 s -> newest IDR with (5 - arrival) >= 2.5 is ts=2000
        buf2 = PacketBuffer(1024 * 1024, 60)
        for t in tags:
            buf2.put(t)
        dropped, idr = buf2.drop_to_target_delay(2.5, now=5.0, is_idr_fn=lambda t: tag_has_idr(t, 4))
        self.assertEqual(dropped, 3)
        self.assertEqual(idr.timestamp, 2000)

    def test_settings_persistence_and_windows_dpapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            store = SettingsStore(path)
            store.save({"preset": "twitch", "default_delay": 45, "auto_reconnect": True})
            loaded = store.load()
            self.assertEqual(loaded["preset"], "twitch")
            self.assertEqual(loaded["default_delay"], 45)
            if os.name == "nt":
                secret = "live_99887766_supersecret_key"
                self.assertTrue(store.save_encrypted_key("twitch", secret))
                raw_json = Path(path).read_text(encoding="utf-8")
                self.assertNotIn(secret, raw_json)
                self.assertEqual(store.load_encrypted_key("twitch"), secret)
                self.assertIn("twitch", store.saved_key_presets())

    def test_icon_ico_generation_and_binary_resolution(self):
        ico = build_ico_bytes(sizes=(16, 32))
        self.assertEqual(ico[:4], b"\x00\x00\x01\x00")
        self.assertGreater(len(ico), 150)
        self.assertTrue(resolve_binary("ffmpeg"))


if __name__ == "__main__":
    unittest.main()
