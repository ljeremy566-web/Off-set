"""Hito B multi-cycle live RTMP endurance test:
4 consecutive live transitions in ONE uninterrupted RTMP session with BT.709 + bf=2:
  1) LIVE (0s) -> Enable Delay (2.0s via custom PNG slate with BT.709 + AUD)
  2) DELAYED (2.0s) -> Elastic delay increase to 3.5s
  3) DELAYED (3.5s) -> Anti-leak Purge (-1.0s) -> Lossless Catch-Up (1.5x) back to LIVE (0s)
  4) LIVE (0s) -> Panic Slate Hold (1.2s) -> Jump Live (0s) -> FINISHED

Asserts: 0 receiver disconnects, 0 FFmpeg decode errors, strictly monotonic DTS across all splices.
Run: python -m tests.test_hito_b_endurance
"""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from offset.relay import Config, Relay
from offset.slate import SlateStore
from tests.test_dynamic_integration import make_custom_slate_png, verify_clean_decode_and_monotonic_dts


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix="offset-hitob-") as tmp:
        root = Path(tmp)
        slate_png = root / "custom_slate.png"
        make_custom_slate_png(slate_png)
        store = SlateStore(root / "slate")
        store.save_image(slate_png.read_bytes(), "custom_slate.png")

        in_p, out_p = free_port(), free_port()
        while out_p == in_p:
            out_p = free_port()
        in_url = f"rtmp://127.0.0.1:{in_p}/live/input"
        out_url = f"rtmp://127.0.0.1:{out_p}/live/offset"
        out_flv = root / "hitob_multicycle.flv"
        rlog = (root / "recv.log").open("wb")
        receiver = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-listen", "1", "-i", out_url,
             "-map", "0:v", "-map", "0:a", "-c", "copy", "-f", "flv", str(out_flv)],
            stdout=subprocess.DEVNULL, stderr=rlog,
        )
        relay = Relay(
            Config(delay=0, default_delay=2.0, catchup_rate=1.5,
                   input_url=in_url, output_url=out_url, allow_bframes=True, auto_reconnect=False),
            slate_store=store,
        )
        pub = None
        try:
            relay.start()
            time.sleep(0.6)
            # 22 s BT.709 H.264 bf=2 + AAC stream (matches real Twitch/YouTube OBS output!)
            pub = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
                 "avsynctest=size=640x360:framerate=30:samplerate=48000:period=1[out0][out1]",
                 "-map", "0:v", "-map", "0:a", "-t", "22",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                 "-bf", "2", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-f", "flv", in_url],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )

            # Wait for LIVE
            t0 = time.perf_counter()
            while relay.sent < 35 and not relay.done.is_set() and time.perf_counter() - t0 < 10:
                time.sleep(0.05)
            assert relay.state == "RELAYING" and relay.delay_mode == "live", relay.snapshot()

            # Cycle 1: LIVE -> Enable Delay 2.0s
            relay.enable_delay(2.0)
            t1 = time.perf_counter()
            while relay.delay_mode != "delayed" and not relay.done.is_set() and time.perf_counter() - t1 < 8:
                time.sleep(0.05)
            assert relay.delay_mode == "delayed", relay.snapshot()

            # Cycle 2: Elastic increase 2.0s -> 3.5s
            time.sleep(0.5)
            relay.set_delay(3.5)
            t2 = time.perf_counter()
            while relay.delay_mode != "delayed" and not relay.done.is_set() and time.perf_counter() - t2 < 8:
                time.sleep(0.05)
            assert relay.delay_mode == "delayed" and relay.target_delay == 3.5, relay.snapshot()

            # Cycle 3: Anti-leak Purge (-1.0s) + Lossless Catch-up back to LIVE (0s)
            time.sleep(0.5)
            relay.purge(seconds=1.0)
            relay.disable_delay(mode="catchup", catchup_rate=1.5)
            t3 = time.perf_counter()
            while relay.delay_mode != "live" and not relay.done.is_set() and time.perf_counter() - t3 < 10:
                time.sleep(0.05)
            assert relay.delay_mode == "live", relay.snapshot()

            # Cycle 4: Panic Slate Hold (1.2s) -> Jump Live
            relay.hold_slate()
            t4 = time.perf_counter()
            while relay.delay_mode != "slate_hold" and not relay.done.is_set() and time.perf_counter() - t4 < 5:
                time.sleep(0.05)
            assert relay.delay_mode == "slate_hold", relay.snapshot()
            time.sleep(1.2)
            relay.release_slate(mode="jump")

            pub.wait(timeout=25)
            assert relay.done.wait(25), relay.snapshot()
            receiver.wait(timeout=10)
            snap = relay.snapshot()
            assert snap["state"] == "FINISHED", snap
            assert receiver.returncode == 0
            counts = verify_clean_decode_and_monotonic_dts(out_flv)
            print(json.dumps({
                "cycles_completed": 4,
                "state": snap["state"],
                "sent_packets": snap["sent_packets"],
                "slate_packets_sent": snap["slate_packets_sent"],
                "purged_packets": snap["purged_packets"],
                "decoded_packets": counts,
                "decode_errors": 0,
            }, indent=2, ensure_ascii=False))
        finally:
            relay.stop()
            for p in (pub, receiver):
                if p and p.poll() is None:
                    p.terminate(); p.wait(timeout=5)
            rlog.close()


if __name__ == "__main__":
    main()
