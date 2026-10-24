import asyncio
import unittest
import subprocess
from unittest.mock import patch
from bili_cli.asr.segment import Segment
from live_stream import run_pipeline, decoded_pcm

class FakeVAD:
    def __init__(self): self.count = 0
    def feed(self, pcm):
        self.count += 1
        return [Segment(self.count-1, self.count, pcm)]
    def flush(self): return []

class Engine:
    def __init__(self, delay=0, fail=False): self.delay, self.fail, self.active = delay, fail, 0
    async def transcribe(self, segments):
        self.active += 1
        try:
            await asyncio.sleep(self.delay)
            if self.fail: raise RuntimeError('recognition failed')
            return ['test']
        finally: self.active -= 1

class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_end(self):
        async def source(_):
            yield b'\x01\x00' * 512
        events=[]
        result=await run_pipeline(source, FakeVAD, Engine(), max_reconnects=0, emit=events.append)
        self.assertEqual(result['reason'], 'stream_end')
        self.assertEqual(result['segments'],1)
        self.assertEqual(events[-2]['event'], 'transcript')

    async def test_reconnect_and_pcm_dedup(self):
        async def source(_): yield b'\x01\x00' * 512
        result=await run_pipeline(source,FakeVAD,Engine(),max_reconnects=2,reconnect_delay=0,emit=lambda e:None)
        self.assertEqual(result['reconnects'],2)
        self.assertEqual(result['duplicates'],2)
        self.assertEqual(result['segments'],1)

    async def test_same_session_repeat_is_not_duplicate(self):
        async def source(_):
            for i in range(2):
                yield b'\x01\x00' * 512
                await asyncio.sleep(.001)
        result=await run_pipeline(source,FakeVAD,Engine(),max_reconnects=0,emit=lambda e:None)
        self.assertEqual(result['segments'],2)
        self.assertEqual(result['duplicates'],0)

    async def test_backlog_bounded(self):
        async def source(_):
            for i in range(30): yield bytes([i,0])*512
        events=[]
        result=await run_pipeline(source,FakeVAD,Engine(.001),queue_size=2,max_reconnects=0,emit=events.append)
        self.assertEqual(result['segments'],2)
        self.assertEqual(result['dropped'],28)
        self.assertEqual(len([e for e in events if e['event']=='gap']),28)

    async def test_stream_error_reconnect_bounded(self):
        async def source(_):
            yield b'\x00\x00'*512
            raise ConnectionError('secret URL must not escape')
        events=[]
        result=await run_pipeline(source,FakeVAD,Engine(),max_reconnects=1,reconnect_delay=0,emit=events.append)
        self.assertEqual(result['reason'],'stream_error')
        self.assertNotIn('secret',str(events))

    async def test_cancel_waits_current_and_drops_backlog(self):
        async def source(_):
            for i in range(30):
                yield bytes([i,0])*512
                await asyncio.sleep(.001)
            await asyncio.sleep(100)
        engine=Engine(.08)
        events=[]
        task=asyncio.create_task(run_pipeline(source,FakeVAD,engine,max_reconnects=0,emit=events.append))
        await asyncio.sleep(.04)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,1)
        self.assertEqual(engine.active,0)
        self.assertEqual(events[-1]['reason'],'cancelled')
        self.assertTrue(any(e.get('reason')=='cancelled' for e in events[:-1]))

    async def test_consumer_failure_full_queue_no_deadlock(self):
        async def source(_):
            for i in range(10):
                yield bytes([i,0])*512
                await asyncio.sleep(.001)
            await asyncio.sleep(100)
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(run_pipeline(source,FakeVAD,Engine(.02,True),queue_size=1,emit=lambda e:None),1)

    async def test_vad_constructor_failure_not_swallowed(self):
        async def source(_): yield b'\x00\x00'*512
        def bad(): raise ValueError('missing model')
        with self.assertRaises(ValueError):
            await run_pipeline(source,bad,Engine(),emit=lambda e:None)

    async def test_duration_closes_stream(self):
        closed=[]
        async def source(_):
            try:
                yield b'\x00\x00'*512
                await asyncio.sleep(100)
            finally: closed.append(True)
        result=await run_pipeline(source,FakeVAD,Engine(),duration=.02,emit=lambda e:None)
        self.assertEqual(result['reason'],'duration')
        self.assertEqual(closed,[True])

    async def test_httpx_pipe_decodes_flv_without_network(self):
        import httpx
        fixture=subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
            'sine=frequency=440:sample_rate=44100','-t','0.3','-c:a','libmp3lame','-f','flv','pipe:1'],
            check=True,capture_output=True).stdout
        real_client=httpx.AsyncClient
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,content=fixture)
        transport=httpx.MockTransport(handler)
        def client(**kwargs):
            return real_client(**kwargs,transport=transport)
        frames=[]
        with patch.object(httpx,'AsyncClient',client):
            async for pcm in decoded_pcm(url='https://cdn.example.test/live.flv?token=private'):
                frames.append(pcm)
        self.assertGreater(sum(map(len,frames)),3200)
        self.assertTrue(all(len(p)<=1024 for p in frames))
        self.assertNotIn('cookie',requests[0].headers)

    async def test_bounds(self):
        with self.assertRaises(ValueError):
            await run_pipeline(None,None,None,queue_size=100)

if __name__=='__main__': unittest.main()
