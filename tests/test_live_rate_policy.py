"""Offline live-only rate policy boundaries, using the real shared guard."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_danmaku as module
from bili_cli.errors import BiliError


class LiveRatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ledger = module._Ledger(self.home)
        self.now = 10000.0
        self.original = dict(module.guard.LIMITS)
        for p in (patch.object(module.config, 'home', return_value=self.home),
                  patch.object(module.time, 'time', return_value=self.now),
                  patch.object(module.guard, '_disabled', return_value=False),
                  patch('bili_cli.output.settings.wait', False)):
            p.start()
            self.addCleanup(p.stop)

    def seed(self, count, *, shared=False, age=20):
        times = [self.now - age - 11 * n for n in reversed(range(count))]
        entries = {str(n): {'started_at': t, 'fingerprint': str(n),
                           'result': {'status': 'uncertain'}} for n, t in enumerate(times)}
        self.ledger.write(entries)
        (self.home / 'ratelimit.json').write_text(json.dumps({'danmaku': times if shared else []}))

    def claim(self):
        return self.ledger.claim('new', 'new', {'status': 'in_flight'})

    def test_100th_allowed_101st_rejected_and_history_retained(self):
        self.seed(99, shared=True)
        old = json.loads(self.ledger.path.read_text())
        shared = (self.home / 'ratelimit.json').read_bytes()
        self.assertIsNone(self.claim())
        current = json.loads(self.ledger.path.read_text())
        self.assertEqual(len(current), 100)
        self.assertTrue(all(current[k] == v for k, v in old.items()))
        with patch.object(module.time, 'time', return_value=self.now + 11):
            with self.assertRaises(BiliError):
                self.ledger.claim('next', 'next', {'status': 'in_flight'})
        self.assertEqual((self.home / 'ratelimit.json').read_bytes(), shared)
        self.assertEqual(module.guard.LIMITS, self.original)

    def test_shared_gate_100th_allowed_101st_rejected(self):
        self.seed(99, shared=True)
        self.ledger.write({})
        self.assertIsNone(self.claim())
        self.seed(100, shared=True)
        self.ledger.write({})
        with self.assertRaises(BiliError) as caught:
            self.claim()
        self.assertEqual(caught.exception.detail['limit']['per_hour'], 100)
        self.assertEqual(module.guard.LIMITS, self.original)

    def test_rolling_boundary_expired_entries_preserved(self):
        self.seed(100, shared=True, age=3600)
        self.assertIsNone(self.claim())
        self.assertEqual(len(json.loads(self.ledger.path.read_text())), 101)

    def test_default_ten_seconds_both_gates(self):
        for shared_only in (False, True):
            self.seed(1, shared=True, age=9.999)
            if shared_only:
                self.ledger.write({})
            with self.assertRaises(BiliError):
                self.claim()
            self.assertEqual(module.guard.LIMITS, self.original)
        self.seed(1, shared=True, age=10)
        self.assertIsNone(self.claim())

    def test_non_live_limits_still_apply(self):
        self.seed(30, shared=True)
        with self.assertRaises(BiliError):
            module.guard.check('danmaku')
        self.assertIsNone(self.claim())
        self.assertEqual(module.guard.LIMITS, self.original)
        with self.assertRaises(BiliError):
            module.guard.check('danmaku')
        self.assertEqual(module.guard.LIMITS['danmaku'], (10, 30))
        self.assertEqual(module.guard.LIMITS['comment'], (10, 30))

    def test_scoped_trial_cap_is_still_five(self):
        with self.assertRaises(BiliError):
            module.ScopedTestSession(room_id=123, expected_uid='456', text='test',
                                     key_prefix='offline', expires_at=self.now + 60, count=6)


if __name__ == '__main__':
    unittest.main()
