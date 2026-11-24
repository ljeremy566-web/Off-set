"""Real codecs and RTMP. No external accounts; fresh temporary files.

Run from the project root: python -m tests.integration
"""
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import socket
import subprocess
import tempfile
import time

from offset.media import read_tags, Validator, MediaError
from offset.relay import Config, Relay


def run(command, timeout=30):
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors='replace')[-1800:])
    return result.stdout


def fixture(path, bf=0, seconds=4):
    run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
         'avsynctest=size=640x360:framerate=30:samplerate=48000:period=1[out0][out1]',
         '-map', '0:v', '-map', '0:a', '-t', str(seconds), '-c:v', 'libx264',
         '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-bf', str(bf),
         '-g', '60', '-keyint_min', '60', '-sc_threshold', '0',
         '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-f', 'flv', str(path)])


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_packets', '-show_streams',
                          '-show_data_hash', 'sha256', '-of', 'json', str(path)]))


def assert_preserved(source, dest):
    a, b = probe(source), probe(dest)
    assert len(a['packets']) == len(b['packets']), (len(a['packets']), len(b['packets']))
    offsets, last = set(), {}
    for original, result in zip(a['packets'], b['packets']):
        assert original['data_hash'] == result['data_hash'], 'Payload cambiado'
        assert original['codec_type'] == result['codec_type']
        offsets.add(round(float(result['dts_time'])-float(original['dts_time']), 3))
        kind, ts = result['codec_type'], int(result['dts'])
        assert kind not in last or ts > last[kind], 'DTS no creciente'
        last[kind] = ts
    assert len(offsets) == 1, ('Se alteró el desfase entre pistas', offsets)
    errors = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(dest), '-f', 'null', '-'],
                            capture_output=True, timeout=30)
    assert errors.returncode == 0 and not errors.stderr, errors.stderr
    return {'packets': len(b['packets']), 'payloads_identical': True,
            'common_timestamp_offset_s': offsets.pop(), 'decode_errors': 0}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def wait_relay(relay, timeout=25):
    if not relay.done.wait(timeout):
        relay.stop(); raise AssertionError('Timeout del relay')
    report = relay.snapshot()
    assert report['state'] == 'FINISHED', report
    return report


def main():
    report = {}
    with tempfile.TemporaryDirectory(prefix='offset-test-') as tmp:
        root = Path(tmp)
        source = root/'source.flv'; fixture(source)
        # Exact compressed payload identity and common A/V timestamps, no codecs
        # exercised by the relay itself. Audit asserts the per-packet delay floor.
        out, audit = root/'file-output.flv', root/'audit.jsonl'
        relay = Relay(Config(delay=.4), input_file=source, output_file=out, audit_file=audit)
        try:
            relay.start(); result = wait_relay(relay)
            ages = [json.loads(line)['residence_s'] for line in audit.read_text().splitlines()]
            assert min(ages) >= .4
            report['file_delay'] = {**assert_preserved(source, out),
                                    'minimum_residence_s': min(ages),
                                    'buffer_peak_bytes': result['buffer']['peak_bytes']}
        finally:
            relay.stop()
        # Two independent protections against B frames, using real encoder data.
        bfile = root/'bframes.flv'; fixture(bfile, bf=2, seconds=1)
        for zero_cts in (False, True):
            v = Validator(); rejected = None
            try:
                with bfile.open('rb') as stream:
                    for tag in read_tags(stream):
                        if zero_cts and tag.kind == 9 and tag.payload[1] == 1:
                            tag = replace(tag, payload=tag.payload[:2]+b'\x00'*3+tag.payload[5:])
                        v.accept(tag)
            except MediaError as exc:
                rejected = str(exc)
            assert rejected and ('B-frames detectados' in rejected if zero_cts else 'PTS distinto' in rejected)
            report['bframes_zero_cts' if zero_cts else 'bframes_normal'] = rejected
        # A deliberately tiny byte cap must fault, not silently jump forward.
        relay = Relay(Config(delay=2), input_file=source, output_file=root/'capacity.flv')
        relay.buffer.max_bytes = 1000
        try:
            relay.start(); assert relay.done.wait(5)
            result = relay.snapshot()
            assert result['state'] == 'FAULT' and result['sent_packets'] == 0, result
            assert result['buffer']['bytes'] <= 1000
            report['capacity'] = 'FAULT antes de enviar; límite respetado'
        finally:
            relay.stop()
        # Actual RTMP: source -> FFmpeg listener -> relay -> FFmpeg RTMP receiver.
        input_port, output_port = free_port(), free_port()
        while output_port == input_port: output_port = free_port()
        incoming = f'rtmp://127.0.0.1:{input_port}/live/input'
        outgoing = f'rtmp://127.0.0.1:{output_port}/live/offset'
        network_out = root/'rtmp-output.flv'
        sinklog = (root/'receiver.log').open('wb')
        receiver = subprocess.Popen(['ffmpeg', '-v', 'error', '-listen', '1', '-i', outgoing,
                                     '-map', '0:v', '-map', '0:a', '-c', 'copy', '-f', 'flv',
                                     str(network_out)], stdout=subprocess.DEVNULL, stderr=sinklog)
        relay = Relay(Config(delay=1, input_url=incoming, output_url=outgoing), audit_file=root/'rtmp-audit.jsonl')
        try:
            relay.start(); time.sleep(.5)
            run(['ffmpeg', '-v', 'error', '-re', '-i', str(source), '-map', '0:v', '-map', '0:a',
                 '-c', 'copy', '-f', 'flv', incoming], timeout=20)
            result = wait_relay(relay)
            receiver.wait(timeout=10)
            assert receiver.returncode == 0
            ages = [json.loads(line)['residence_s'] for line in (root/'rtmp-audit.jsonl').read_text().splitlines()]
            assert min(ages) >= 1
            report['rtmp'] = {**assert_preserved(source, network_out),
                              'minimum_residence_s': min(ages),
                              'rss_peak_bytes_motor_and_transports': result['memory']['rss_peak_bytes']}
        finally:
            relay.stop()
            if receiver.poll() is None: receiver.terminate(); receiver.wait(timeout=5)
            sinklog.close()
        # Missing destination must terminate promptly with an explicit fault.
        relay = Relay(Config(delay=0, output_url=f'rtmp://127.0.0.1:{free_port()}/live/offset'), input_file=source)
        try:
            relay.start(); assert relay.done.wait(15)
            assert relay.state == 'FAULT', relay.snapshot()
            report['destination_missing'] = 'FAULT sin cuelgue'
        finally:
            relay.stop()
        # Actual destination disconnect while a local RTMP stream is active.
        destination = f'rtmp://127.0.0.1:{free_port()}/live/offset'
        receiver = subprocess.Popen(['ffmpeg', '-v', 'error', '-listen', '1', '-i', destination,
                                     '-c', 'copy', '-f', 'null', '-'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        relay = Relay(Config(delay=.2, output_url=destination), input_file=source)
        try:
            relay.start()
            deadline = time.monotonic()+5
            while relay.sent < 100 and not relay.done.is_set() and time.monotonic() < deadline:
                time.sleep(.02)
            assert relay.sent >= 100, relay.snapshot()
            receiver.kill(); receiver.wait(timeout=3)
            assert relay.done.wait(12)
            assert relay.state == 'FAULT', relay.snapshot()
            report['destination_disconnect'] = 'FAULT durante transmisión activa, sin reconexión ni salto'
        finally:
            relay.stop()
            if receiver.poll() is None: receiver.kill(); receiver.wait(timeout=3)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
