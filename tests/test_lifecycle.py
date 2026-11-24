"""Regression tests for shutdown failures and a writer that outlives stop()."""
import threading
import unittest
import io
from pathlib import Path
import json
import re
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from offset.media import Tag
from offset.relay import Config, Relay
from offset.server import Controller, Busy, make_server
from tests.test_core import video, video_header


def ready_relay():
    relay = Relay(Config(delay=0), output_file='unused.flv')
    relay.validator.accept(video_header())
    relay.validator.accept(Tag(8, 0, b'\xaf\x00\x11\x90', 0))
    relay.validator.accept(video())
    relay.validator.accept(Tag(8, 0, b'\xaf\x01\x00', 0))
    relay.buffer.put(video())
    return relay


class LifecycleTests(unittest.TestCase):
    def test_write_and_close_errors_still_release_queue_and_signal_done(self):
        relay = ready_relay()
        relay.egress_thread = threading.current_thread()
        stream = Mock()
        stream.write.side_effect = BrokenPipeError('injected write error')
        stream.close.side_effect = OSError('injected close error')
        with patch('pathlib.Path.open', return_value=stream):
            relay._egress()
        self.assertEqual(relay.state, 'FAULT')
        self.assertTrue(relay.done.is_set())
        self.assertEqual(relay.buffer.snapshot()['packets'], 0)

    def test_input_close_error_still_signals_ingress_done(self):
        relay = Relay(Config(delay=0), input_file='unused.flv')
        stream = Mock()
        stream.close.side_effect = OSError('injected input close error')
        with patch('pathlib.Path.open', return_value=stream), patch('offset.relay.read_tags', return_value=iter(())):
            relay._ingress()
        self.assertTrue(relay.ingress_done.is_set())
        self.assertTrue(relay.buffer.closed)
        self.assertEqual(relay.state, 'FAULT')

    def test_stop_does_not_complete_or_discard_while_writer_is_alive(self):
        relay = ready_relay()
        entered, release = threading.Event(), threading.Event()
        stream = Mock()
        stream.write.side_effect = lambda data: len(data)
        original_write = relay._write

        def blocked_write(dest, data):
            if data == video().encode():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('test writer was not released')
            return original_write(dest, data)

        relay._write = blocked_write
        worker = threading.Thread(target=relay._egress)
        relay.egress_thread, relay.threads = worker, [worker]
        with patch('pathlib.Path.open', return_value=stream):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertIs(relay.stop(timeout=.05), False)
                self.assertTrue(worker.is_alive())
                self.assertFalse(relay.done.is_set())
                self.assertEqual(relay.buffer.snapshot()['packets'], 1)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(relay.done.is_set())
        self.assertEqual(relay.buffer.snapshot()['packets'], 0)

    def test_stopped_session_cannot_return_to_buffering(self):
        relay = Relay(Config())
        relay.stop()
        relay.set_state('BUFFERING')
        self.assertEqual(relay.state, 'STOPPED')

    def test_new_session_is_refused_until_previous_workers_exit(self):
        controller = Controller(Config())
        previous = Relay(Config())
        previous.state = 'FAULT'
        previous.stop = Mock(return_value=False)
        controller.relay = previous
        with patch('offset.server.Relay') as constructor:
            with self.assertRaisesRegex(Busy, 'pendiente'):
                controller.start({})
            constructor.assert_not_called()
        self.assertIs(controller.relay, previous)

    def test_spawn_in_progress_is_published_before_stop_can_finish(self):
        relay = Relay(Config())
        spawning, release, stopping = threading.Event(), threading.Event(), threading.Event()
        proc = Mock()
        proc.stdin, proc.stdout, proc.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        result = {'returncode': None}
        proc.poll.side_effect = lambda: result['returncode']
        proc.terminate.side_effect = lambda: result.update(returncode=0)
        proc.kill.side_effect = lambda: result.update(returncode=-1)
        proc.wait.side_effect = lambda **kwargs: result['returncode']

        def spawn(*args, **kwargs):
            spawning.set()
            if not release.wait(3):
                raise TimeoutError('spawn not released')
            return proc

        def stop():
            stopping.set()
            result['stop_complete'] = relay.stop()

        with patch('offset.relay.subprocess.Popen', side_effect=spawn):
            creator = threading.Thread(target=relay._spawn, args=(['unused'], 'output_proc'))
            closer = threading.Thread(target=stop)
            creator.start()
            self.assertTrue(spawning.wait(1))
            closer.start()
            self.assertTrue(stopping.wait(1))
            release.set()
            creator.join(3); closer.join(3)
        self.assertFalse(creator.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertIs(relay.output_proc, proc)
        self.assertIsNotNone(proc.poll())
        self.assertTrue(result['stop_complete'])
        with patch('offset.relay.subprocess.Popen') as create_again:
            with self.assertRaises(RuntimeError):
                relay._spawn(['unused'], 'input_proc')
            create_again.assert_not_called()

    def test_audit_close_error_is_not_reported_as_success(self):
        relay = ready_relay()
        relay.audit_file = Path('unused-audit.jsonl')
        relay.egress_thread = threading.current_thread()
        relay.ingress_done.set()
        stream, audit = Mock(), Mock()
        stream.write.side_effect = lambda data: len(data)
        audit.close.side_effect = OSError('injected audit close error')
        with patch('pathlib.Path.open', side_effect=[stream, audit]):
            relay._egress()
        self.assertEqual(relay.state, 'FAULT')
        self.assertTrue(relay.done.is_set())
        self.assertEqual(relay.buffer.bytes, 0)

    def test_worker_start_failure_can_be_cleaned_up(self):
        relay = Relay(Config(), input_file='unused.flv', output_file='unused-output.flv')
        with patch('threading.Thread.start', side_effect=RuntimeError('cannot start new thread')):
            with self.assertRaises(RuntimeError):
                relay.start()
        self.assertTrue(relay.done.is_set())
        self.assertEqual(relay.state, 'FAULT')
        self.assertTrue(relay.stop())

    def test_completed_stop_is_not_overwritten_by_late_failure(self):
        relay = Relay(Config())
        self.assertTrue(relay.stop())
        relay.fail('late watchdog result')
        self.assertEqual(relay.state, 'STOPPED')
        self.assertIsNone(relay.error)

    def test_cleanup_of_finished_session_preserves_success(self):
        relay = Relay(Config())
        relay.state = 'FINISHED'
        self.assertTrue(relay.stop())
        relay.fail('late transport result')
        self.assertEqual(relay.state, 'FINISHED')
        self.assertEqual(list(relay.events), [])

    def test_status_is_readable_while_controller_waits_for_stop(self):
        controller = Controller(Config())
        relay = Relay(Config())
        controller.relay = relay
        stopping, release, read = threading.Event(), threading.Event(), threading.Event()

        def stop():
            stopping.set()
            release.wait(3)
            return True

        relay.stop = stop
        closer = threading.Thread(target=controller.stop)
        reader = threading.Thread(target=lambda: (controller.status(), read.set()))
        closer.start()
        try:
            self.assertTrue(stopping.wait(1))
            reader.start()
            self.assertTrue(read.wait(1), 'status blocked behind stop')
        finally:
            release.set()
            closer.join(3); reader.join(3)

    def test_early_wait_wakeup_does_not_dispatch_before_deadline(self):
        relay = Relay(Config())
        clock = iter([0.0, 0.2, 0.8, 1.0])
        with patch('offset.relay.os.name', 'posix'), patch('offset.relay.time.perf_counter', side_effect=lambda: next(clock)), patch.object(relay.stop_event, 'wait', return_value=False) as wait:
            self.assertFalse(relay._wait_until(1.0))
        self.assertEqual(wait.call_count, 3)

    def test_closed_queue_does_not_count_rejected_input(self):
        relay = ready_relay()
        relay.input_file = Path('unused.flv')
        relay.buffer.discard()
        incoming = Tag(8, 21, b'\xaf\x01\x00', 0)
        with patch('pathlib.Path.open', return_value=Mock()), patch('offset.relay.read_tags', return_value=iter([incoming])):
            relay._ingress()
        self.assertEqual(relay.received, 0)
        self.assertEqual(relay.buffer.bytes, 0)

    def test_busy_shutdown_is_http_409_and_does_not_start_new_relay(self):
        server = make_server(Config(), 0)
        previous = Relay(Config())
        previous.state = 'FAULT'
        previous.stop = Mock(return_value=False)
        server.controller.relay = previous
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base) as response:
                token = re.search(r'name="offset-token" content="([^"]+)"', response.read().decode())[1]
            for endpoint in ('start', 'stop'):
                request = Request(base+'/api/'+endpoint, b'{}', headers={'X-Offset-Token': token, 'Content-Type': 'application/json'})
                with self.assertRaises(HTTPError) as denied:
                    urlopen(request)
                self.assertEqual(denied.exception.code, 409)
                self.assertIn('pendiente', json.load(denied.exception)['error'])
                denied.exception.close()
                self.assertIs(server.controller.relay, previous)
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_unresponsive_process_returns_pending_without_raising(self):
        relay = Relay(Config())
        proc = Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired('injected process', 2)
        relay.output_proc = proc
        self.assertIs(relay.stop(), False)
        self.assertEqual(relay.state, 'FAULT')
        self.assertFalse(relay.snapshot()['shutdown_complete'])
        proc.kill.assert_called()

    def test_python_310_is_rejected_before_starting_transports(self):
        with patch('offset.relay.sys.version_info', (3, 10, 12)):
            with self.assertRaisesRegex(ValueError, '3.11'):
                Config().validate()

    def test_clean_input_close_error_does_not_abort_pending_drain(self):
        relay = ready_relay()
        relay.state = 'BUFFERING'
        relay.input_file = Path('unused.flv')
        stream = Mock()
        stream.close.side_effect = OSError('injected close after valid EOF')
        with patch('pathlib.Path.open', return_value=stream), patch('offset.relay.read_tags', return_value=iter(())):
            relay._ingress()
        self.assertEqual(relay.state, 'DRAINING')
        self.assertFalse(relay.stop_event.is_set())
        self.assertTrue(relay.ingress_done.is_set())
        self.assertEqual(relay.buffer.snapshot()['packets'], 1)

    def test_watchdog_result_after_user_stop_does_not_create_fault(self):
        relay = Relay(Config())
        proc = Mock()
        proc.stdin, proc.stdout, proc.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        relay.output_proc = proc
        polling, release = threading.Event(), threading.Event()
        state = {'code': None}

        def poll():
            if threading.current_thread() is watchdog:
                polling.set()
                if not release.wait(3):
                    raise TimeoutError('watchdog not released')
            return state['code']

        proc.poll.side_effect = poll
        proc.terminate.side_effect = lambda: state.update(code=0)
        proc.kill.side_effect = lambda: state.update(code=-1)
        proc.wait.side_effect = lambda **kwargs: state['code']
        watchdog = threading.Thread(target=relay._watchdog)
        relay.threads = [watchdog]
        closer = threading.Thread(target=lambda: state.update(stopped=relay.stop()))
        watchdog.start()
        try:
            self.assertTrue(polling.wait(1))
            closer.start()
            self.assertTrue(relay.stop_event.wait(1))
        finally:
            release.set()
            watchdog.join(3)
            if closer.ident is not None:
                closer.join(3)
        self.assertTrue(state['stopped'])
        self.assertEqual(relay.state, 'STOPPED')
        self.assertIsNone(relay.error)


if __name__ == '__main__':
    unittest.main()
