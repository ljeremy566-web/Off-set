import io
import json
import re
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from offset.buffer import CapacityError, PacketBuffer, Pacer
from offset.media import FLV_HEADER, MediaError, Tag, Validator, read_tags
from offset.relay import Config, local_rtmp
from offset.server import make_server


def video_header():
    sps, pps = b'\x67\x42\x00\x1e', b'\x68\xce\x3c\x80'
    avcc = b'\x01\x42\x00\x1e\xff\xe1' + len(sps).to_bytes(2, 'big') + sps
    avcc += b'\x01' + len(pps).to_bytes(2, 'big') + pps
    return Tag(9, 0, b'\x17\x00\x00\x00\x00' + avcc, 0)


def video(nal=b'\x65\xb0', ts=0, cts=0):
    return Tag(9, ts, b'\x17\x01' + cts.to_bytes(3, 'big') + len(nal).to_bytes(4, 'big') + nal, ts/1000)


class MediaTests(unittest.TestCase):
    def validator(self):
        v = Validator(); v.accept(video_header())
        v.accept(Tag(8, 0, b'\xaf\x00\x11\x90', 0))
        return v

    def test_flv_round_trip(self):
        expected = video(ts=0x12345678)
        actual = list(read_tags(io.BytesIO(FLV_HEADER + expected.encode())))[0]
        self.assertEqual((actual.kind, actual.timestamp, actual.payload),
                         (expected.kind, expected.timestamp, expected.payload))

    def test_truncated_and_oversized_rejected(self):
        for raw in (FLV_HEADER + video().encode()[:-1],
                    FLV_HEADER + b'\x09\xff\xff\xff' + b'\x00'*7):
            with self.assertRaises(MediaError):
                list(read_tags(io.BytesIO(raw)))

    def test_zero_cts_does_not_hide_b_slice(self):
        v = self.validator()
        # first_mb=0, slice_type=1 (B), even though PTS==DTS.
        with self.assertRaisesRegex(MediaError, 'B-frames detectados'):
            v.accept(video(b'\x41\xa0'))

    def test_cts_and_slice_validation_are_separate(self):
        with self.assertRaisesRegex(MediaError, 'PTS distinto'):
            self.validator().accept(video(cts=1))

    def test_first_picture_must_be_idr(self):
        with self.assertRaisesRegex(MediaError, 'IDR'):
            self.validator().accept(video(b'\x41\xe0'))

    def test_valid_idr_and_audio(self):
        v = self.validator()
        self.assertTrue(v.accept(video()))
        self.assertTrue(v.accept(Tag(8, 0, b'\xaf\x01\x00', 0)))
        self.assertTrue(v.ready)

    def test_headers_cannot_change(self):
        v = self.validator()
        with self.assertRaisesRegex(MediaError, 'Configuración'):
            v.accept(Tag(8, 1, b'\xaf\x00\x12\x10', .001))

    def test_monotonic_and_global_order(self):
        v = self.validator(); v.accept(video(ts=20))
        with self.assertRaisesRegex(MediaError, 'crecer'):
            v.accept(video(ts=20))
        with self.assertRaisesRegex(MediaError, 'orden'):
            v.accept(Tag(8, 10, b'\xaf\x01\x00', .01))


class BufferTests(unittest.TestCase):
    def test_byte_cap_does_not_drop_unread_content(self):
        item = video(); b = PacketBuffer(item.size, 30); b.put(item)
        with self.assertRaises(CapacityError):
            b.put(video(ts=1))
        self.assertIs(b.peek(), item)
        self.assertEqual(b.snapshot()['packets'], 1)

    def test_duration_and_packet_caps(self):
        b = PacketBuffer(10000, 1); b.put(video())
        with self.assertRaises(CapacityError):
            b.put(video(ts=1001))
        b = PacketBuffer(10000, 100, max_packets=1); b.put(video())
        with self.assertRaises(CapacityError):
            b.put(video(ts=1))

    def test_residence_floor(self):
        p = Pacer(10)
        self.assertEqual(p.deadline(Tag(9, 0, b'', 5)), 15)

    def test_stall_reanchors_without_catchup(self):
        p = Pacer(2)
        p.sent(Tag(9, 0, b'', 0), 12)
        self.assertEqual(p.deadline(Tag(9, 1000, b'', 1)), 13)

    def test_scheduler_jitter_does_not_accumulate(self):
        p = Pacer(2)
        for n in range(1000):
            t = Tag(9, n*10, b'', n*.01)
            p.sent(t, p.deadline(t)+.001)
        self.assertAlmostEqual(p.last_sent, 11.99)


class ControlTests(unittest.TestCase):
    def test_only_explicit_loopback_rtmp(self):
        local_rtmp('rtmp://127.0.0.1:1935/live/input')
        for url in ('rtmp://example.com:1935/live/key', 'file:///etc/passwd',
                    'rtmp://127.0.0.1:1935/live/key?secret=x', 'rtmp://localhost:1935/live/key'):
            with self.assertRaises(ValueError): local_rtmp(url)

    def test_invalid_config(self):
        for delay in (float('nan'), float('inf'), -1, 121):
            with self.assertRaises(ValueError): Config(delay=delay).validate()

    def test_http_origin_token_and_static(self):
        server = make_server(Config(), 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        root = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(root) as response:
                html = response.read().decode()
                self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            token = re.search(r'name="offset-token" content="([^"]+)"', html)[1]
            with self.assertRaises(HTTPError) as denied:
                urlopen(root+'/api/status')
            self.assertEqual(denied.exception.code, 403)
            request = Request(root+'/api/status', headers={'X-Offset-Token': token})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)['state'], 'READY')
            request = Request(root+'/api/stop', b'{}', headers={
                'X-Offset-Token': token, 'Origin': 'https://evil.example', 'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as denied:
                urlopen(request)
            self.assertEqual(denied.exception.code, 403)
        finally:
            server.shutdown(); server.server_close(); worker.join()


if __name__ == '__main__':
    unittest.main()
