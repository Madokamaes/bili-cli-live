"""Offline final-wire-form tests: mock HTTPX transport + socket deny guard.
No real credentials are read and no request reaches the network.
"""
import asyncio
import inspect
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from bilibili_api import Credential, select_client
from bilibili_api.video import Video
from bilibili_api.clients.HTTPXClient import HTTPXClient
from bilibili_api.exceptions import ResponseCodeException
from bilibili_api.utils import network
import coin_form_patch

ORIGINAL_SIGNATURE = inspect.signature(Video.pay_coin)
coin_form_patch.apply()
COIN = '/x/web-interface/coin/add'
LIKE = '/x/web-interface/archive/like'
DUMMY = Credential(sessdata='offline-sess', bili_jct='offline-csrf', dedeuserid='1', buvid3='offline-b3', buvid4='offline-b4')

class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response_code = 0
        def handler(request):
            self.requests.append(request)
            if request.url.path not in (COIN, LIKE):
                raise AssertionError('Unexpected outbound target in offline test')
            return httpx.Response(200, json={'code': self.response_code, 'message': 'mock', 'data': {}}, request=request)
        self.session = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        self.client = HTTPXClient(session=self.session)
        self.patches = [
            patch.object(network, 'get_client', return_value=self.client),
            patch.object(socket.socket, 'connect', side_effect=AssertionError('Network prohibited')),
            patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('Network prohibited')),
        ]
        for p in self.patches: p.start()
        # Constructor resolves AID from BVID locally; no metadata request.
        self.video = Video(bvid='BV1aU5W6ZEkX', credential=DUMMY)

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        await self.session.aclose()

    def form(self, expected_path):
        request = self.requests[-1]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.url.path, expected_path)
        self.assertEqual(request.headers['content-type'], 'application/x-www-form-urlencoded')
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        self.assertEqual(form['csrf'], ['offline-csrf'])
        self.assertEqual(form['csrf_token'], ['offline-csrf'])
        return form

    async def test_coin_select_like_zero_one_and_quantity(self):
        for number in (1, 2):
            for enabled in (False, True):
                await self.video.pay_coin(num=number, like=enabled)
                form = self.form(COIN)
                self.assertEqual(form['select_like'], [str(int(enabled))])
                self.assertNotIn('like', form)
                self.assertEqual(form['multiply'], [str(number)])
                self.assertIn('aid', form)
                self.assertEqual(form['bvid'], ['BV1aU5W6ZEkX'])
        self.assertEqual(len(self.requests), 4)

    async def test_independent_like_unchanged(self):
        for enabled, value in ((True, '1'), (False, '2')):
            await self.video.like(status=enabled)
            form = self.form(LIKE)
            self.assertEqual(form['like'], [value])
            self.assertNotIn('select_like', form)

    async def test_business_failures_are_single_attempt(self):
        for code in (-401, -403):
            for action in (lambda: self.video.pay_coin(num=1, like=False), lambda: self.video.like()):
                self.response_code = code
                before = len(self.requests)
                with self.assertRaises(ResponseCodeException) as raised:
                    await action()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(self.requests) - before, 1)

    async def test_invalid_quantity_does_not_submit(self):
        for number in (0, 3):
            with self.assertRaises(Exception):
                await self.video.pay_coin(num=number)
        self.assertEqual(len(self.requests), 0)

    async def test_signature_and_idempotence(self):
        self.assertEqual(inspect.signature(Video.pay_coin), ORIGINAL_SIGNATURE)
        original = Video.pay_coin
        coin_form_patch.apply()
        self.assertIs(Video.pay_coin, original)

class CliWireTests(unittest.TestCase):
    def test_cli_like_option_and_failure_no_retry(self):
        from typer.testing import CliRunner
        from bili_cli.commands import video as commands
        from bili_cli import guard, output
        from contextlib import ExitStack
        runner = CliRunner()
        for enabled in (False, True):
            for code in (0, -401, -403):
                requests = []
                def handler(request):
                    requests.append(request)
                    if request.url.path != COIN:
                        raise AssertionError('Unexpected CLI outbound target')
                    return httpx.Response(200, json={'code': code, 'message': 'mock', 'data': {}}, request=request)
                session = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
                client = HTTPXClient(session=session)
                async def relation(*args, **kwargs):
                    return {'coins': 0, 'liked': False, 'favorited': False}
                v = Video(bvid='BV1aU5W6ZEkX', credential=DUMMY)
                with ExitStack() as stack:
                    stack.enter_context(patch.object(network, 'get_client', return_value=client))
                    stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('Network prohibited')))
                    stack.enter_context(patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('Network prohibited')))
                    stack.enter_context(patch.object(commands, '_own_video', return_value=(v, 'BV1aU5W6ZEkX')))
                    stack.enter_context(patch.object(commands, '_relation', side_effect=relation))
                    stack.enter_context(patch.object(commands, '_watch_note', return_value={}))
                    stack.enter_context(patch.object(guard, 'check'))
                    stack.enter_context(patch.object(guard, 'record'))
                    stack.enter_context(patch.object(output.settings, 'dry_run', False))
                    args = ['coin', 'BV1aU5W6ZEkX', '--coins', '1'] + (['--like'] if enabled else [])
                    result = runner.invoke(commands.app, args)
                asyncio.run(session.aclose())
                self.assertEqual(result.exit_code, 0 if code == 0 else (5 if code == -403 else 1), result.output)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(request.headers['content-type'], 'application/x-www-form-urlencoded')
                form = parse_qs(request.content.decode())
                self.assertEqual(form['select_like'], [str(int(enabled))])
                self.assertEqual(form['multiply'], ['1'])
                self.assertNotIn('like', form)

if __name__ == '__main__':
    unittest.main(verbosity=2)
