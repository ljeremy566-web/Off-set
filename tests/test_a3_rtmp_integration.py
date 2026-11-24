"""Real live RTMP end-to-end integration tests for Hito A3:
  1) Live RTMP (FFmpeg publisher bf=2 -> Offset -> RTMP FFmpeg receiver):
     LIVE -> Custom PNG Slate Delay -> Anti-Leak Purge -> Panic Slate Hold -> Jump Live
  2) Live RTMP OBS Crash & Auto-Reconnect:
     Publisher #1 drops -> Offset keeps outgoing RTMP alive with slate -> Publisher #2 connects ->
     Single continuous output FLV with 0 receiver disconnects and 0 decode errors!

Run: python -m tests.test_a3_rtmp_integration
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


def start_rtmp_receiver(url, out_path, log_path):
    log = log_path.open("wb")
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-listen", "1", "-i", url,
         "-map", "0:v", "-map", "0:a", "-c", "copy", "-f", "flv", str(out_path)],
        stdout=subprocess.DEVNULL, stderr=log,
    )
    return proc, log


def start_rtmp_publisher(url, seconds):
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
         "avsynctest=size=640x360:framerate=30:samplerate=48000:period=1[out0][out1]",
         "-map", "0:v", "-map", "0:a", "-t", str(seconds),
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-bf", "2", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-f", "flv", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def main():
    report = {}
    with tempfile.TemporaryDirectory(prefix="offset-a3-rtmp-") as tmp:
        root = Path(tmp)
        slate_png = root / "custom_slate.png"
        make_custom_slate_png(slate_png)
        store = SlateStore(root / "slate")
        store.save_image(slate_png.read_bytes(), "custom_slate.png")

        # --- SCENARIO 1: Live RTMP dynamic delay + anti-leak purge + panic slate ---
        in_p, out_p = free_port(), free_port()
        while out_p == in_p:
            out_p = free_port()
        in_url = f"rtmp://127.0.0.1:{in_p}/live/input"
        out_url = f"rtmp://127.0.0.1:{out_p}/live/offset"
        out_flv = root / "a3_live_rtmp.flv"
        receiver, rlog = start_rtmp_receiver(out_url, out_flv, root / "recv1.log")
        relay = Relay(
            Config(delay=0, default_delay=2.5, catchup_rate=1.4,
                   input_url=in_url, output_url=out_url, allow_bframes=True, auto_reconnect=False),
            slate_store=store,
        )
        pub = None
        try:
            relay.start()
            time.sleep(0.6)
            pub = start_rtmp_publisher(in_url, seconds=14)
            t0 = time.perf_counter()
            while relay.sent < 40 and not relay.done.is_set() and time.perf_counter() - t0 < 10:
                time.sleep(0.05)
            assert relay.state == "RELAYING", relay.snapshot()

            # 1) Enable 2.5 s delay with custom slate
            relay.enable_delay(2.5)
            t1 = time.perf_counter()
            while relay.delay_mode != "delayed" and not relay.done.is_set() and time.perf_counter() - t1 < 10:
                time.sleep(0.05)
            assert relay.delay_mode == "delayed", relay.snapshot()

            # 2) Anti-leak purge of last 1.0 s from write head
            time.sleep(0.6)
            snap_purge = relay.purge(seconds=1.0)
            assert snap_purge["purged_packets"] > 0, snap_purge

            # 3) Immediate Panic Slate Hold for 1.5 s, then release live
            relay.hold_slate()
            t2 = time.perf_counter()
            while relay.delay_mode != "slate_hold" and not relay.done.is_set() and time.perf_counter() - t2 < 5:
                time.sleep(0.05)
            assert relay.delay_mode == "slate_hold", relay.snapshot()
            time.sleep(1.5)
            relay.release_slate(mode="jump")

            pub.wait(timeout=20)
            assert relay.done.wait(20), relay.snapshot()
            receiver.wait(timeout=10)
            snap = relay.snapshot()
            assert snap["state"] == "FINISHED", snap
            assert receiver.returncode == 0
            counts = verify_clean_decode_and_monotonic_dts(out_flv)
            report["rtmp_dynamic_purge_panic"] = {
                "state": snap["state"],
                "sent_packets": snap["sent_packets"],
                "slate_packets_sent": snap["slate_packets_sent"],
                "purged_packets": snap["purged_packets"],
                "decoded_packets": counts,
                "decode_errors": 0,
            }
        finally:
            relay.stop()
            for p in (pub, receiver):
                if p and p.poll() is None:
                    p.terminate(); p.wait(timeout=5)
            rlog.close()

        # --- SCENARIO 2: Live RTMP OBS Drop & Auto-Reconnect (outgoing stream stays ALIVE) ---
        in_p2, out_p2 = free_port(), free_port()
        while out_p2 == in_p2:
            out_p2 = free_port()
        in_url2 = f"rtmp://127.0.0.1:{in_p2}/live/input"
        out_url2 = f"rtmp://127.0.0.1:{out_p2}/live/offset"
        out_flv2 = root / "a3_reconnect_rtmp.flv"
        receiver2, rlog2 = start_rtmp_receiver(out_url2, out_flv2, root / "recv2.log")
        relay2 = Relay(
            Config(delay=0, default_delay=2.0, input_url=in_url2, output_url=out_url2,
                   allow_bframes=True, auto_reconnect=True, reconnect_timeout=6.0),
            slate_store=store,
        )
        pub1 = pub2 = None
        try:
            relay2.start()
            time.sleep(0.6)
            # Epoch 1: 3 seconds then OBS drops!
            pub1 = start_rtmp_publisher(in_url2, seconds=3)
            pub1.wait(timeout=15)

            # Verify Offset entered input reconnect mode & kept receiver alive with slate!
            t0 = time.perf_counter()
            saw_reconn = False
            while time.perf_counter() - t0 < 5:
                st = relay2.snapshot()
                if st["reconnects"]["input_reconnecting"] or st["delay_mode"] == "reconnecting_input":
                    saw_reconn = True
                    break
                time.sleep(0.05)
            assert saw_reconn, relay2.snapshot()
            assert receiver2.poll() is None, "Receiver disconnected during OBS reconnect!"

            # Wait 1.2 s (slate keeps RTMP alive!), then Epoch 2 OBS reconnects for 3 s!
            time.sleep(1.2)
            pub2 = start_rtmp_publisher(in_url2, seconds=3)
            pub2.wait(timeout=15)

            # Wait until epoch 2 was accepted and packets flowed, then let reconnect timeout finish cleanly
            t1 = time.perf_counter()
            while relay2.input_reconnects < 1 and time.perf_counter() - t1 < 5:
                time.sleep(0.05)
            assert relay2.input_reconnects >= 1, relay2.snapshot()
            assert relay2.done.wait(15), relay2.snapshot()
            receiver2.wait(timeout=10)
            snap2 = relay2.snapshot()
            assert snap2["state"] == "FINISHED", snap2
            assert receiver2.returncode == 0
            counts2 = verify_clean_decode_and_monotonic_dts(out_flv2)
            report["rtmp_obs_auto_reconnect"] = {
                "state": snap2["state"],
                "input_reconnects": snap2["reconnects"]["input"],
                "slate_packets_sent": snap2["slate_packets_sent"],
                "sent_packets": snap2["sent_packets"],
                "decoded_packets": counts2,
                "decode_errors": 0,
            }
        finally:
            relay2.stop()
            for p in (pub1, pub2, receiver2):
                if p and p.poll() is None:
                    p.terminate(); p.wait(timeout=5)
            rlog2.close()

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
