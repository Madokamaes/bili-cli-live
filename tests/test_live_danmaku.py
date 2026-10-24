"""Offline wire/ledger/socket tests. No real credentials or live calls."""
import asyncio
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from bilibili_api import Credential
from bilibili_api.clients.HTTPXClient import HTTPXClient
from bilibili_api.utils import network
from bili_cli.errors import BiliError
import live_danmaku as module

CRED = Credential(sessdata='test-session', bili_jct='test-csrf', buvid3='test-buvid3',
                  buvid4='test-buvid4', dedeuserid='123456789')


class LiveSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.requests = []
        self.payload = {'code': 0, 'message': '', 'data': {}}
        self.http_status = 200
        self.raise_error = None
        self.play = AsyncMock(return_value={'room_id': 456, 'uid': 789, 'live_status': 1, 'is_locked': False})
        def handler(request):
            self.requests.append(request)
            self.assertEqual(request.url.path, '/msg/send')
            self.assertEqual(request.url.host, 'api.live.bilibili.com')
            if self.raise_error:
                raise self.raise_error
            return httpx.Response(self.http_status, json=self.payload, request=request)
        self.session = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        self.client = HTTPXClient(session=self.session)
        self.patches = [patch.object(module.config, 'home', return_value=Path(self.temp.name)),
                        patch.object(module.config, 'require_credential', return_value=CRED),
                        patch.object(module.config, 'optional_credential', return_value=CRED),
                        patch.object(module.live.LiveRoom, 'get_room_play_info', self.play),
                        patch.object(module.guard, 'check'), patch.object(module.guard, 'record'),
                        patch.object(network, 'get_client', return_value=self.client),
                        patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')),
                        patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network forbidden'))]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        await self.session.aclose()
        self.temp.cleanup()

    async def send(self, key='test-key', **kwargs):
        return await module.send(123, '测试弹幕', idempotency_key=key, **kwargs)

    async def test_one_post_form_and_success_not_echo(self):
        result = await self.send()
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, 'POST')
        self.assertTrue(request.headers['content-type'].startswith('application/x-www-form-urlencoded'))
        form = parse_qs(request.content.decode())
        self.assertEqual(form['msg'], ['测试弹幕'])
        self.assertEqual(form['roomid'], ['456'])
        self.assertEqual(form['mode'], ['1'])
        self.assertEqual(form['fontsize'], ['25'])
        self.assertEqual(form['color'], ['16777215'])
        self.assertEqual(form['csrf'], ['test-csrf'])
        self.assertEqual(form['csrf_token'], ['test-csrf'])
        self.assertIs(result['business_success'], True)
        self.assertIs(result['echo_observed'], False)
        self.assertEqual(result['status'], 'accepted')
        contents = (Path(self.temp.name) / 'live_danmaku_ledger.json').read_text()
        self.assertNotIn('test-session', contents)
        self.assertNotIn('test-csrf', contents)
        self.assertNotIn('测试弹幕', contents)
        self.assertEqual(os.stat(Path(self.temp.name) / 'live_danmaku_ledger.json').st_mode & 0o777, 0o600)

    async def test_duplicate_replays_without_network_or_metadata(self):
        first = await self.send()
        self.play.reset_mock()
        second = await self.send()
        self.assertEqual(first['status'], second['status'])
        self.assertTrue(second['idempotent_replay'])
        self.assertEqual(len(self.requests), 1)
        self.play.assert_not_awaited()

    async def test_conflicting_key_does_not_send(self):
        await self.send()
        with self.assertRaises(BiliError):
            await module.send(123, '不同弹幕', idempotency_key='test-key')
        self.assertEqual(len(self.requests), 1)

    async def test_timeout_is_uncertain_and_duplicate_never_resubmits(self):
        self.raise_error = httpx.ReadTimeout('redacted', request=httpx.Request('POST', module.SEND_URL))
        result = await self.send()
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['business_success'])
        self.assertFalse(result['retry_allowed'])
        repeated = await self.send()
        self.assertEqual(repeated['status'], 'uncertain')
        self.assertEqual(len(self.requests), 1)

    async def test_business_rejection_single_attempt(self):
        self.payload = {'code': -403, 'message': 'server-private-message', 'data': {'token': 'secret'}}
        result = await self.send()
        self.assertIs(result['business_success'], False)
        self.assertEqual(result['status'], 'rejected')
        self.assertNotIn('server-private-message', json.dumps(result))
        self.assertNotIn('secret', json.dumps(result))
        await self.send()
        self.assertEqual(len(self.requests), 1)

    async def test_code_zero_nonempty_rejection_not_claimed_success(self):
        self.payload = {'code': 0, 'msg': 'msg repeat', 'data': {}}
        result = await self.send()
        self.assertIsNone(result['business_success'])
        self.assertEqual(result['status'], 'uncertain')
        await self.send()
        self.assertEqual(len(self.requests), 1)

    async def test_redirect_not_followed(self):
        self.http_status = 302
        result = await self.send()
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(self.requests), 1)

    async def test_boolean_code_not_success(self):
        self.payload = {'code': False, 'data': {}}
        self.assertIsNone((await self.send())['business_success'])

    async def test_dry_run_offline_no_credentials_or_ledger(self):
        module.config.require_credential.side_effect = AssertionError('must not load credentials')
        result = await self.send(dry_run=True)
        self.assertEqual(result['status'], 'not_sent')
        self.assertEqual(self.requests, [])
        self.play.assert_not_awaited()
        self.assertFalse((Path(self.temp.name) / 'live_danmaku_ledger.json').exists())

    async def test_explicit_target_text_key_required(self):
        for room, text, key in [(0, 'a', 'key'), (True, 'a', 'key'), ('https://evil/123', 'a', 'key'),
                                (123, ' ', 'key'), (123, 'a\nb', 'key'), (123, 'a', ''), (123, 'a'*101, 'key')]:
            with self.assertRaises(BiliError):
                await module.send(room, text, idempotency_key=key)
        self.assertEqual(self.requests, [])
        self.play.assert_not_awaited()

    async def test_wrong_account_blocks_before_network(self):
        with self.assertRaises(BiliError):
            await self.send(expected_uid='1')
        self.assertEqual(self.requests, [])
        self.play.assert_not_awaited()

    async def test_offline_room_blocks_submission(self):
        self.play.return_value['live_status'] = 0
        with self.assertRaises(BiliError):
            await self.send()
        self.assertEqual(self.requests, [])

    async def test_corrupt_ledger_fails_closed(self):
        (Path(self.temp.name) / 'live_danmaku_ledger.json').write_text('{broken')
        with self.assertRaises(BiliError):
            await self.send()
        self.assertEqual(self.requests, [])
        self.play.assert_not_awaited()

    async def test_abandoned_intent_is_not_retried(self):
        params = {'requested_room_id': 123, 'text': '测试弹幕', 'color': 'ffffff', 'mode': 'scroll',
                  'sender_uid': str(CRED.dedeuserid)}
        key = module.hashlib.sha256(b'test-key').hexdigest()
        ledger = module._Ledger()
        ledger.claim(key, module._fingerprint(params), {'status': 'in_flight'})
        result = await self.send()
        self.assertEqual(result['status'], 'uncertain')
        self.assertTrue(result['idempotent_replay'])
        self.assertEqual(self.requests, [])

    async def test_second_new_key_throttled_after_uncertain_attempt(self):
        self.raise_error = TimeoutError('offline timeout')
        await self.send()
        with self.assertRaises(BiliError) as error:
            await self.send(key='second-key')
        self.assertEqual(error.exception.code, 'rate_limited')
        self.assertEqual(len(self.requests), 1)

    async def test_echo_setup_failure_does_not_retry_send(self):
        with patch.object(module._Observer, 'start', AsyncMock(side_effect=TimeoutError)), patch.object(module._Observer, 'close', AsyncMock()):
            result = await self.send(verify_echo=True)
        self.assertTrue(result['business_success'])
        self.assertFalse(result['echo_observed'])
        self.assertEqual(result['echo_status'], 'unavailable')
        self.assertEqual(len(self.requests), 1)

    async def test_echo_success_separate_from_business(self):
        with patch.object(module._Observer, 'start', AsyncMock()), patch.object(module._Observer, 'close', AsyncMock()), patch.object(module._Observer, 'wait_echo', AsyncMock(return_value=True)):
            result = await self.send(verify_echo=True)
        self.assertTrue(result['business_success'])
        self.assertTrue(result['echo_observed'])
        self.assertEqual(len(self.requests), 1)

    async def test_cancel_during_submit_keeps_uncertain_durable(self):
        with patch.object(self.client, 'request', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.send()
        result = await self.send()
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['reason'], 'cancelled_during_submission')
        self.assertEqual(self.requests, [])

    async def test_cancel_during_echo_setup_closes_without_send(self):
        with patch.object(module._Observer, 'start', AsyncMock(side_effect=asyncio.CancelledError)), patch.object(module._Observer, 'close', AsyncMock()) as close:
            with self.assertRaises(asyncio.CancelledError):
                await self.send(verify_echo=True)
            close.assert_awaited_once()
        self.assertEqual(self.requests, [])
        self.assertFalse((Path(self.temp.name) / 'live_danmaku_ledger.json').exists())

    async def test_concurrent_same_key_has_only_one_submission(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.client.request
        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return await original(**kwargs)
        with patch.object(self.client, 'request', delayed):
            first = asyncio.create_task(self.send())
            await entered.wait()
            second = await self.send()
            self.assertEqual(second['status'], 'uncertain')
            self.assertTrue(second['idempotent_replay'])
            release.set()
            result = await first
        self.assertTrue(result['business_success'])
        self.assertEqual(len(self.requests), 1)

    async def test_durable_intent_failure_prevents_post(self):
        with patch.object(module._Ledger, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.send()
        self.assertEqual(self.requests, [])

    async def test_credential_proxy_fails_closed(self):
        with patch.object(CRED, 'proxy', 'http://proxy.invalid:8080'):
            with self.assertRaises(BiliError):
                await self.send()
        self.assertEqual(self.requests, [])
        self.play.assert_not_awaited()

    async def test_room_info_redacts_stream_details(self):
        details = {'room_info': {'title': 'title'}, 'anchor_info': {'base_info': {'uname': 'anchor'}}, 'token': 'secret'}
        with patch.object(module.live.LiveRoom, 'get_room_info', AsyncMock(return_value=details)):
            result = await module.room_info(123)
        self.assertEqual(result['room_id'], 456)
        self.assertEqual(result['uname'], 'anchor')
        self.assertNotIn('secret', json.dumps(result))

    async def test_stream_url_exact_protocol_codec(self):
        data = {'live_status': 1, 'playurl_info': {'playurl': {'stream': [
            {'protocol_name': 'http_stream', 'format': [{'format_name': 'flv', 'codec': [
                {'codec_name': 'avc', 'base_url': '/stream.flv', 'url_info': [{'host': 'https://example.bilivideo.com', 'extra': '?token=test'}]}]}]}]}}}
        with patch.object(module.live.LiveRoom, 'get_room_play_info_v2', AsyncMock(return_value=data)) as read:
            url = await module.get_stream_url(123)
        self.assertEqual(url, 'https://example.bilivideo.com/stream.flv?token=test')
        self.assertEqual(read.call_args.kwargs['live_codec'], module.live.LiveCodec.AVC)


class ParserTests(unittest.IsolatedAsyncioTestCase):
    def packet(self, payload, op=5, protocol=0):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return struct.pack('>IHHII', 16 + len(body), 16, protocol, op, 1) + body

    def message(self, text='test', uid='123', timestamp=None):
        return {'cmd': 'DANMU_MSG:4:0:2:2:2:0', 'info': [[0, 1, 25, 0, timestamp or time.time()], text, [uid, 'name']]}

    async def test_parser_simple_compressed_and_malformed(self):
        packet = self.packet(self.message())
        self.assertEqual(len(module._events(packet)), 1)
        self.assertEqual(len(module._events(self.packet(zlib.compress(packet), protocol=2))), 1)
        for bad in (b'', b'bad', struct.pack('>IHHII', 0, 16, 0, 5, 1), packet[:-1]):
            if not bad:
                self.assertEqual(module._events(bad), [])
            else:
                with self.assertRaises(ValueError):
                    module._events(bad)
        with self.assertRaises(ValueError):
            module._events(self.packet(zlib.compress(b'a' * (4*1024*1024+1)), protocol=2))

    async def test_read_echo_requires_uid_text_and_fresh_timestamp(self):
        from curl_cffi import CurlWsFlag
        observer = module._Observer(456, CRED)
        sent = time.time()
        observer.arm(sent, '123', 'test')
        messages = [self.message(uid='999'), self.message(text='other'), self.message(timestamp=sent - 10)]
        class WS:
            def __init__(self, frames): self.frames = frames
            async def recv(self):
                if self.frames:
                    return self.frames.pop(0), CurlWsFlag.BINARY
                return b'', CurlWsFlag.CLOSE
        observer.ws = WS([self.packet(x) for x in messages])
        await observer._read()
        self.assertFalse(observer.echo.is_set())
        observer.ws = WS([self.packet(self.message(timestamp=sent))])
        await observer._read()
        self.assertTrue(observer.echo.is_set())

    async def test_aggregate_compression_and_event_budget(self):
        # Each sibling is below the per-frame cap, but together exceeds it.
        body = self.packet({'cmd': 'NOTICE', 'padding': 'x' * (3 * 1024 * 1024)})
        compressed = self.packet(zlib.compress(body), protocol=2)
        with self.assertRaises(ValueError):
            module._events(compressed + compressed)
        with self.assertRaises(ValueError):
            module._events(self.packet({}) * 4097)
        huge = self.message(text='x' * 4097)
        self.assertIsNone(module._message(huge, 456))

    async def test_socket_auth_timeout_cleans_waiter_and_private_session(self):
        from curl_cffi import requests
        observer = module._Observer(456, CRED)
        class WS:
            async def send_binary(self, data): pass
            async def recv(self):
                await asyncio.Event().wait()
            async def close(self): pass
        ws = WS()
        session = type('Session', (), {})()
        session.ws_connect = AsyncMock(return_value=ws)
        session.close = AsyncMock()
        conf = {'token': 'private-token', 'host_list': [{'host': 'broadcast.chat.bilibili.com', 'wss_port': 443}]}
        before = set(asyncio.all_tasks())
        selected = network.get_selected_client()[0]
        with patch.object(requests, 'AsyncSession', return_value=session), patch.object(module.live.LiveRoom, 'get_danmu_info', AsyncMock(return_value=conf)):
            try:
                with self.assertRaises(TimeoutError):
                    await observer.start(timeout=0.02)
            finally:
                await observer.close()
        await asyncio.sleep(0)
        self.assertEqual(set(asyncio.all_tasks()) - before, set())
        session.close.assert_awaited_once()
        self.assertEqual(network.get_selected_client()[0], selected)

    async def test_recall_not_confused_with_echo(self):
        message = self.message()
        message['cmd'] = 'RECALL_DANMU_MSG'
        self.assertIsNone(module._message(message, 456))


if __name__ == '__main__':
    unittest.main()
