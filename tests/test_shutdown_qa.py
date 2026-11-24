import unittest
from offset.buffer import PacketBuffer
from offset.media import Tag
from offset.relay import Relay,Config

class ShutdownRegression(unittest.TestCase):
    def test_normal_eof_preserves_queue_for_drain(self):
        b=PacketBuffer(10000,40);b.put(Tag(9,0,b'payload',0));b.close()
        self.assertEqual(b.snapshot()['packets'],1)
        self.assertEqual(b.pop().payload,b'payload')
    def test_stop_releases_pending_payloads(self):
        r=Relay(Config());r.buffer.put(Tag(9,0,b'payload',0));r.stop()
        self.assertEqual(r.state,'STOPPED');self.assertEqual(r.buffer.snapshot()['packets'],0);self.assertEqual(r.buffer.bytes,0)
    def test_failure_before_worker_start_releases_payloads(self):
        r=Relay(Config());r.buffer.put(Tag(9,0,b'payload',0));r.fail('test failure')
        self.assertEqual(r.state,'FAULT');self.assertEqual(r.buffer.snapshot()['packets'],0);self.assertEqual(r.buffer.bytes,0)

class DeadlineRegression(unittest.TestCase):
    def test_wait_preserves_deadline_and_obeys_cancel(self):
        import time
        r=Relay(Config());deadline=time.perf_counter()+.02
        self.assertFalse(r._wait_until(deadline))
        self.assertGreaterEqual(time.perf_counter(),deadline)
        r.stop_event.set()
        self.assertTrue(r._wait_until(time.perf_counter()+10))
