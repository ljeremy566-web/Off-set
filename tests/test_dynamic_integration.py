"""Real FFmpeg end-to-end integration tests for dynamic delay transitions.

Tests with REAL H.264 (including B-frames=2 like default OBS!) + AAC + custom PNG slate:
  1) LIVE (0s) -> Enable Delay (2s via custom PNG slate, 0 OBS frames lost) -> Lossless Catch-Up -> LIVE (0s)
  2) DELAYED (2.5s) -> Jump to Live ("Directo ya" at next IDR) -> LIVE (0s)

Run: python -m tests.test_dynamic_integration
"""
import json
from pathlib import Path
import subprocess
import tempfile
import time

from offset.media import read_tags
from offset.relay import Config, Relay
from offset.slate import SlateStore


def run(cmd, timeout=30):
    res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if res.returncode != 0:
        raise AssertionError(res.stderr.decode(errors="replace")[-1500:])
    return res.stdout


def make_bframe_fixture(path, seconds=10):
    run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "avsynctest=size=640x360:framerate=30:samplerate=48000:period=1[out0][out1]",
        "-map", "0:v", "-map", "0:a", "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-bf", "2", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-f", "flv", str(path),
    ])


def make_custom_slate_png(path):
    run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=640x360:rate=1", "-frames:v", "1", str(path),
    ])


def verify_clean_decode_and_monotonic_dts(flv_path):
    # Full decode must succeed with 0 errors
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(flv_path), "-f", "null", "-"],
                         capture_output=True, timeout=30)
    assert dec.returncode == 0 and not dec.stderr, dec.stderr.decode(errors="replace")

    probe = json.loads(run([
        "ffprobe", "-v", "error", "-show_packets", "-show_streams", "-of", "json", str(flv_path)
    ]))
    last_dts = {}
    counts = {"video": 0, "audio": 0}
    for pkt in probe["packets"]:
        ct = pkt["codec_type"]
        counts[ct] = counts.get(ct, 0) + 1
        dts = int(pkt["dts"])
        if ct in last_dts:
            assert dts >= last_dts[ct], f"Non-monotonic DTS on {ct}: {dts} < {last_dts[ct]}"
        last_dts[ct] = dts
    assert counts["video"] > 0 and counts["audio"] > 0, counts
    return counts


def count_source_media_tags(flv_path):
    total = 0
    with flv_path.open("rb") as f:
        for tag in read_tags(f):
            if tag.kind == 9 and len(tag.payload) >= 2 and tag.payload[1] == 1:
                total += 1
            elif tag.kind == 8 and len(tag.payload) >= 2 and tag.payload[1] == 1:
                total += 1
    return total


def main():
    report = {}
    with tempfile.TemporaryDirectory(prefix="offset-dyn-") as tmp:
        root = Path(tmp)
        source = root / "obs_bframes.flv"
        slate_png = root / "custom_slate.png"
        make_bframe_fixture(source, seconds=10)
        make_custom_slate_png(slate_png)
        expected_media_tags = count_source_media_tags(source)

        store = SlateStore(root / "slate_store")
        store.save_image(slate_png.read_bytes(), "custom_slate.png")

        # 1) LIVE (0s) -> Enable Delay (2s via custom PNG slate) -> Lossless Catch-Up (1.5x) -> LIVE
        out1 = root / "dynamic_lossless.flv"
        relay1 = Relay(
            Config(delay=0, default_delay=2.0, catchup_rate=1.5, allow_bframes=True),
            input_file=source, output_file=out1, slate_store=store,
        )
        try:
            relay1.start()
            # Wait until LIVE relaying
            t0 = time.perf_counter()
            while relay1.sent < 30 and not relay1.done.is_set() and time.perf_counter() - t0 < 8:
                time.sleep(0.05)
            assert relay1.state == "RELAYING" and relay1.delay_mode == "live", relay1.snapshot()

            # Trigger dynamic delay ON (2.0 s) with custom slate
            relay1.enable_delay(2.0)
            saw_slating = False
            saw_delayed = False
            t1 = time.perf_counter()
            while not relay1.done.is_set() and time.perf_counter() - t1 < 8:
                dm = relay1.delay_mode
                if dm == "slating":
                    saw_slating = True
                if dm == "delayed":
                    saw_delayed = True
                    break
                time.sleep(0.05)
            assert saw_delayed, relay1.snapshot()
            snap_mid = relay1.snapshot()
            assert snap_mid["slate_packets_sent"] > 0, snap_mid
            assert snap_mid["slate"]["is_custom"] is True, snap_mid

            # Trigger lossless catch-up back to 0 s
            time.sleep(0.5)
            relay1.disable_delay(mode="catchup", catchup_rate=1.5)
            assert relay1.done.wait(25), relay1.snapshot()
            snap1 = relay1.snapshot()
            assert snap1["state"] == "FINISHED", snap1
            assert snap1["sent_packets"] == expected_media_tags, (snap1["sent_packets"], expected_media_tags)
            assert snap1["dropped_jump_packets"] == 0, snap1
            assert snap1["stream"]["has_bframes"] is True, snap1
            counts1 = verify_clean_decode_and_monotonic_dts(out1)
            report["live_slate_delayed_catchup_lossless"] = {
                "obs_packets_expected": expected_media_tags,
                "obs_packets_sent": snap1["sent_packets"],
                "obs_packets_lost": expected_media_tags - snap1["sent_packets"],
                "slate_packets_sent": snap1["slate_packets_sent"],
                "custom_slate_used": snap1["slate"]["is_custom"],
                "saw_slating": saw_slating,
                "bframes_handled": snap1["stream"]["has_bframes"],
                "decoded_packets": counts1,
                "decode_errors": 0,
            }
        finally:
            relay1.stop()

        # 2) DELAYED (2.5s) -> Jump to Live ("Directo ya" at next IDR) -> LIVE
        out2 = root / "dynamic_jump.flv"
        relay2 = Relay(
            Config(delay=2.5, allow_bframes=True),
            input_file=source, output_file=out2, slate_store=store,
        )
        try:
            relay2.start()
            t0 = time.perf_counter()
            while relay2.sent < 20 and not relay2.done.is_set() and time.perf_counter() - t0 < 10:
                time.sleep(0.05)
            assert relay2.state == "RELAYING", relay2.snapshot()
            age_before = relay2.snapshot()["last_packet_residence_s"]
            assert age_before >= 2.4, age_before

            # Jump straight to live at latest IDR
            relay2.disable_delay(mode="jump")
            t1 = time.perf_counter()
            while relay2.delay_mode != "live" and not relay2.done.is_set() and time.perf_counter() - t1 < 5:
                time.sleep(0.05)
            assert relay2.delay_mode == "live", relay2.snapshot()
            time.sleep(0.5)
            age_after = relay2.snapshot()["last_packet_residence_s"]
            assert age_after < age_before - 1.0, (age_before, age_after)

            assert relay2.done.wait(20), relay2.snapshot()
            snap2 = relay2.snapshot()
            assert snap2["state"] == "FINISHED", snap2
            assert snap2["dropped_jump_packets"] > 0, snap2
            counts2 = verify_clean_decode_and_monotonic_dts(out2)
            report["delayed_to_jump_live"] = {
                "residence_before_s": round(age_before, 3),
                "residence_after_s": round(age_after, 3),
                "dropped_jump_packets": snap2["dropped_jump_packets"],
                "decoded_packets": counts2,
                "decode_errors": 0,
            }
        finally:
            relay2.stop()

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
