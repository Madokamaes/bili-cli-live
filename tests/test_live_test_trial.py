"""Scoped interval tests use mock transport only, never real B站 calls."""
import time
from unittest.mock import patch
from test_live_danmaku import LiveSendTests, module, CRED
from bili_cli.errors import BiliError


class ScopedTrialTests(LiveSendTests):
    def trial_session(self, **kw):
        values = dict(room_id=456, expected_uid=CRED.dedeuserid, text='🙂',
                      key_prefix='offline-trial', expires_at=time.time()+300)
        values.update(kw)
        return module.ScopedTestSession(**values)

    async def test_five_only_and_one_precheck(self):
        s = self.trial_session()
        base = time.time()
        for n in range(1, 6):
            with patch.object(module.time, 'time', return_value=base+n*2):
                self.assertEqual((await s.send(f'offline-trial-{n}'))['status'], 'accepted')
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(self.play.await_count, 1)
        with self.assertRaises(BiliError):
            await s.send('offline-trial-6')
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(module.guard.LIMITS['danmaku'], (10,30))

    async def test_scope_and_canonical(self):
        s = self.trial_session(room_id=123)
        with self.assertRaises(BiliError):
            await s.send('offline-trial-1')
        self.assertEqual(len(self.requests), 0)

    async def test_stop_uncertain(self):
        s = self.trial_session()
        self.payload = {'code':0, 'msg':'msg repeat'}
        self.assertEqual((await s.send('offline-trial-1'))['status'], 'uncertain')
        with self.assertRaises(BiliError):
            await s.send('offline-trial-2')
        self.assertEqual(len(self.requests), 1)

    async def test_expiry_and_concurrency(self):
        s = self.trial_session()
        s.busy = True
        with self.assertRaises(BiliError):
            await s.send('offline-trial-1')
        s.busy = False
        with patch.object(module.time, 'time', return_value=s.expires_at+1):
            with self.assertRaises(BiliError):
                await s.send('offline-trial-1')
        self.assertEqual(len(self.requests), 0)

    async def test_durable_failure_stops_recreated_session(self):
        s = self.trial_session()
        self.payload = {'code':-403}
        await s.send('offline-trial-1')
        second = self.trial_session()
        with patch.object(module.time, 'time', return_value=time.time()+2):
            with self.assertRaises(BiliError):
                await second.send('offline-trial-2')
        self.assertEqual(len(self.requests), 1)

    async def test_shared_hourly_gate_and_restore(self):
        s = self.trial_session()
        module.guard.check.side_effect = BiliError('rate_limited', 'hourly test')
        with self.assertRaises(BiliError):
            await s.send('offline-trial-1')
        self.assertEqual(module.guard.LIMITS['danmaku'], (10,30))
        self.assertEqual(len(self.requests), 0)

    async def test_default_ten_seconds_not_overridden(self):
        await self.send('normal-one')
        with patch.object(module.time, 'time', return_value=time.time()+2):
            with self.assertRaises(BiliError):
                await self.send('normal-two')
        self.assertEqual(len(self.requests), 1)


import asyncio
import contextlib
import io
import unittest
from unittest.mock import AsyncMock
import live_test_trial as runner


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_monotonic_sequential_and_unknown_stop(self):
        for fail in (False, True):
            starts = []
            active = 0
            maximum = 0
            class FakeSession:
                keys = {'key-1', 'key-2'}
                interval = 1.0
                def __init__(self, **kw):
                    pass
                async def send(self, key):
                    nonlocal active, maximum
                    starts.append(time.monotonic())
                    active += 1
                    maximum = max(maximum, active)
                    await asyncio.sleep(.01)
                    active -= 1
                    return {'status': 'uncertain' if fail else 'accepted'}
            with patch.object(runner, 'ScopedTestSession', FakeSession), \
                 patch.object(runner, 'select_client'), \
                 patch.object(runner.network, 'get_client') as client, \
                 contextlib.redirect_stdout(io.StringIO()):
                client.return_value.close = AsyncMock()
                await runner.run({})
            self.assertEqual(maximum, 1)
            self.assertEqual(len(starts), 1 if fail else 2)
            if not fail:
                self.assertGreaterEqual(starts[1]-starts[0], 1)

    async def test_timeout_stops_once(self):
        calls = []
        class FakeSession:
            keys = {'key-1', 'key-2'}
            interval = 1.0
            def __init__(self, **kw):
                pass
            async def send(self, key):
                calls.append(key)
                raise TimeoutError()
        with patch.object(runner, 'ScopedTestSession', FakeSession), \
             patch.object(runner, 'select_client'), \
             patch.object(runner.network, 'get_client') as client, \
             contextlib.redirect_stdout(io.StringIO()):
            client.return_value.close = AsyncMock()
            await runner.run({})
        self.assertEqual(calls, ['key-1'])
