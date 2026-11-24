"""Measure a real 120-second compressed buffer, or a custom short run.

Run: python -m tests.benchmark --delay 120 --seconds 150
The source is a short, pre-encoded CBR clip looped with stream copy.
The benchmark stops explicitly; it does not claim a 60-minute endurance pass.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

from offset.relay import Config, Relay
from .integration import free_port, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--delay', type=float, default=120)
    parser.add_argument('--seconds', type=float, default=150)
    args = parser.parse_args()
    if args.seconds < args.delay + 5:
        parser.error('La duración debe superar el retraso en al menos 5 segundos.')
    with tempfile.TemporaryDirectory(prefix='offset-benchmark-') as tmp:
        root = Path(tmp)
        source = root/'cbr.flv'
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=60',
             '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000', '-t', '2',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-bf', '0',
             '-g', '120', '-keyint_min', '120', '-sc_threshold', '0',
             '-b:v', '6000k', '-minrate', '6000k', '-maxrate', '6000k', '-bufsize', '6000k',
             '-x264-params', 'nal-hrd=cbr', '-c:a', 'aac', '-b:a', '160k', '-f', 'flv', str(source)], timeout=60)
        input_port, output_port = free_port(), free_port()
        while output_port == input_port: output_port = free_port()
        incoming = f'rtmp://127.0.0.1:{input_port}/live/input'
        outgoing = f'rtmp://127.0.0.1:{output_port}/live/offset'
        logs = [(root/f'{name}.log').open('wb') for name in ('receiver', 'publisher')]
        receiver = subprocess.Popen(['ffmpeg', '-v', 'error', '-listen', '1', '-i', outgoing,
                                     '-c', 'copy', '-f', 'null', '-'],
                                    stdout=subprocess.DEVNULL, stderr=logs[0])
        relay = Relay(Config(delay=args.delay, input_url=incoming, output_url=outgoing))
        publisher = None
        samples = []
        try:
            relay.start(); time.sleep(.5)
            publisher = subprocess.Popen(['ffmpeg', '-v', 'error', '-re', '-stream_loop', '-1',
                                          '-i', str(source), '-c', 'copy', '-f', 'flv', incoming],
                                         stdout=subprocess.DEVNULL, stderr=logs[1])
            start = time.monotonic()
            while time.monotonic()-start < args.seconds:
                time.sleep(1)
                value = relay.snapshot()
                if value['state'] == 'FAULT':
                    raise AssertionError(value)
                if publisher.poll() is not None:
                    raise AssertionError('La fuente terminó antes de tiempo.')
                samples.append({'t': round(time.monotonic()-start, 3), 'state': value['state'],
                                'buffer_bytes': value['buffer']['bytes'],
                                'residence_s': value['last_packet_residence_s'],
                                'sent': value['sent_packets'], **value['memory']})
            assert samples[-1]['sent'] > 0
            ages = [s['residence_s'] for s in samples if s['residence_s'] is not None]
            assert min(ages) >= args.delay
            cpu = [s for s in samples if s['python_cpu_seconds'] is not None]
            own_cpu_pct = ((cpu[-1]['python_cpu_seconds']-cpu[0]['python_cpu_seconds']) /
                           (cpu[-1]['t']-cpu[0]['t'])*100) if len(cpu) > 1 else None
            report = {'duration_s': samples[-1]['t'], 'configured_delay_s': args.delay,
                      'source_configuration': '1920x1080 60 fps H.264 bf=0; objetivo CBR 6 Mbps + AAC 160 kbps (bitrate efectivo no medido)',
                      'buffer_peak_bytes': max(s['buffer_bytes'] for s in samples),
                      'minimum_observed_residence_s': min(ages),
                      'python_average_one_core_cpu_percent': own_cpu_pct,
                      'python_peak_rss_bytes': samples[-1]['python_peak_rss_bytes'],
                      'combined_rss_available': samples[-1]['rss_bytes'] is not None,
                      'note': 'CPU Python excluye FFmpeg. No es una prueba de resistencia de 60 minutos.',
                      'samples': samples}
            print(json.dumps(report, indent=2))
        finally:
            relay.stop()
            for proc in (publisher, receiver):
                if proc and proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            for log in logs: log.close()


if __name__ == '__main__':
    main()
