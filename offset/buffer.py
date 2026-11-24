"""One bounded compressed-packet queue with dynamic delay, IDR jump, and anti-leak purge."""
from collections import deque
import threading


class CapacityError(RuntimeError):
    pass


class PacketBuffer:
    def __init__(self, max_bytes, max_seconds, max_packets=30000):
        self.max_bytes, self.max_seconds = max_bytes, max_seconds
        self.max_packets = max_packets
        self.items = deque()
        self.bytes = self.peak_bytes = 0
        self.lock = threading.Condition()
        self.closed = False

    def set_max_seconds(self, max_seconds):
        with self.lock:
            self.max_seconds = max(self.max_seconds, float(max_seconds))

    def put(self, tag):
        with self.lock:
            if self.closed:
                return False
            span = (tag.timestamp - self.items[0].timestamp) / 1000 if self.items else 0
            residence = tag.arrival - self.items[0].arrival if self.items else 0
            if (self.bytes + tag.size > self.max_bytes or span > self.max_seconds
                    or residence > self.max_seconds or len(self.items) >= self.max_packets):
                raise CapacityError("Buffer lleno: sesión detenida sin adelantar contenido.")
            self.items.append(tag)
            self.bytes += tag.size
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            self.lock.notify_all()
            return True

    def peek(self):
        with self.lock:
            return self.items[0] if self.items else None

    def pop(self):
        with self.lock:
            tag = self.items.popleft()
            self.bytes -= tag.size
            return tag

    def drop_until_latest_idr(self, is_idr_fn):
        """Explicit user jump-to-live: drop everything older than the newest IDR.

        Returns (dropped_count, target_idr_tag) or (0, None) if no IDR is queued.
        Audio with timestamp >= target_idr.timestamp is preserved for A/V alignment.
        """
        with self.lock:
            if not self.items:
                return 0, None
            target_idx = None
            target_idr = None
            for idx in range(len(self.items) - 1, -1, -1):
                item = self.items[idx]
                if is_idr_fn(item):
                    target_idx = idx
                    target_idr = item
                    break
            if target_idr is None:
                return 0, None
            return self._drop_before_index_locked(target_idx, target_idr)

    def drop_to_target_delay(self, target_delay_s, now, is_idr_fn):
        """Partial jump: advance to the newest IDR whose residence is still >= target_delay_s."""
        with self.lock:
            if not self.items:
                return 0, None
            target_idx = None
            target_idr = None
            for idx in range(len(self.items) - 1, -1, -1):
                item = self.items[idx]
                if is_idr_fn(item) and (now - item.arrival) >= target_delay_s:
                    target_idx = idx
                    target_idr = item
                    break
            if target_idr is None or target_idx == 0:
                return 0, None
            return self._drop_before_index_locked(target_idx, target_idr)

    def _drop_before_index_locked(self, target_idx, target_idr):
        cut_ts = target_idr.timestamp
        kept = deque()
        kept_bytes = 0
        dropped = 0
        for idx, item in enumerate(self.items):
            if idx < target_idx and item.timestamp < cut_ts:
                dropped += 1
                continue
            if idx < target_idx and item.kind == 9:
                dropped += 1
                continue
            kept.append(item)
            kept_bytes += item.size
        self.items = kept
        self.bytes = kept_bytes
        self.lock.notify_all()
        return dropped, target_idr

    def purge_recent(self, seconds, is_idr_fn):
        """Anti-leak purge: remove the newest `seconds` from the write head (most recent tail).

        Aligns the cut to the last clean GOP boundary before the incident window so no
        orphaned frames remain. Returns (purged_packets, purged_seconds, emptied_all).
        """
        with self.lock:
            if not self.items:
                return 0, 0.0, True
            newest_ts = self.items[-1].timestamp
            oldest_ts = self.items[0].timestamp
            cutoff_ts = newest_ts - int(round(float(seconds) * 1000))

            if cutoff_ts <= oldest_ts:
                count = len(self.items)
                span = max(0.0, (newest_ts - oldest_ts) / 1000.0)
                self.items.clear()
                self.bytes = 0
                self.lock.notify_all()
                return count, span, True

            # Find the latest IDR at or before cutoff_ts; keep everything strictly BEFORE that IDR
            # (or if no IDR <= cutoff_ts, purge entire queue to be 100% safe against leaks).
            cut_idx = None
            for idx in range(len(self.items) - 1, -1, -1):
                item = self.items[idx]
                if item.timestamp <= cutoff_ts and is_idr_fn(item):
                    cut_idx = idx
                    break

            if cut_idx is None or cut_idx == 0:
                count = len(self.items)
                span = max(0.0, (newest_ts - oldest_ts) / 1000.0)
                self.items.clear()
                self.bytes = 0
                self.lock.notify_all()
                return count, span, True

            cut_ts = self.items[cut_idx].timestamp
            kept = deque()
            kept_bytes = 0
            purged = 0
            for idx, item in enumerate(self.items):
                if idx < cut_idx and item.timestamp < cut_ts:
                    kept.append(item)
                    kept_bytes += item.size
                else:
                    purged += 1
            purged_span = max(0.0, (newest_ts - cut_ts) / 1000.0)
            self.items = kept
            self.bytes = kept_bytes
            self.lock.notify_all()
            return purged, purged_span, len(kept) == 0

    def purge_all(self):
        """Remove all queued unread packets without closing the buffer."""
        with self.lock:
            count = len(self.items)
            span = ((self.items[-1].timestamp - self.items[0].timestamp) / 1000.0
                    if len(self.items) > 1 else 0.0)
            self.items.clear()
            self.bytes = 0
            self.lock.notify_all()
            return count, span

    def close(self):
        with self.lock:
            self.closed = True
            self.lock.notify_all()

    def discard(self):
        """Release pending payloads only after the single writer has exited."""
        with self.lock:
            self.closed = True
            self.items.clear()
            self.bytes = 0
            self.lock.notify_all()

    def snapshot(self):
        with self.lock:
            return {"bytes": self.bytes, "peak_bytes": self.peak_bytes,
                    "packets": len(self.items), "limit_bytes": self.max_bytes,
                    "seconds": (self.items[-1].timestamp - self.items[0].timestamp) / 1000
                    if len(self.items) > 1 else 0}


class Pacer:
    """Never dispatch before residence delay or faster than configured media rate.

    A late dispatch rebases the next deadline, preventing post-stall catch-up.
    The clock anchor changes; media timestamps never change.
    During explicit catch-up (rate > 1.0), dispatches slightly faster than 1x
    wall-clock while preserving valid 1x media timestamps and zero dropped frames.
    """
    def __init__(self, delay, *, rate=1.0, jitter_tolerance=0.025):
        self.delay = float(delay)
        self.rate = float(rate)
        self.jitter_tolerance = float(jitter_tolerance)
        self.last_ts = self.last_sent = None

    def set_delay(self, delay):
        self.delay = float(delay)

    def set_rate(self, rate):
        rate = float(rate)
        if not 1.0 <= rate <= 2.0:
            raise ValueError("La velocidad de alcance debe estar entre 1.0x y 2.0x.")
        self.rate = rate

    def rebase(self):
        self.last_ts = self.last_sent = None

    def deadline(self, tag):
        due = tag.arrival + self.delay
        if self.last_ts is not None:
            delta_s = (tag.timestamp - self.last_ts) / (1000.0 * self.rate)
            due = max(due, self.last_sent + delta_s)
        return due

    def sent(self, tag, now):
        due = self.deadline(tag)
        # Absorb bounded OS scheduler jitter instead of accumulating staircase
        # drift over long sessions. Larger stalls reanchor immediately.
        self.last_ts = tag.timestamp
        self.last_sent = due if 0 <= now - due <= self.jitter_tolerance else now
