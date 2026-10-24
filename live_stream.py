"""Bounded local live ASR. External integration; upstream packages are untouched.

NDJSON events deliberately exclude URLs, cookies, headers and exception strings.
Media timestamps are per connection, not broadcaster timestamps. Queue lag is NOT
end-to-end broadcast latency. CPU recognition is short-segment, not token streaming.
"""
from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import time
from pathlib import Path

RATE = 16000
FRAME = 512
VERSION = 'live-v1'


def emit_json(event):
    print(json.dumps({'schema': VERSION, **event}, ensure_ascii=False), flush=True)


class StreamingVAD:
    def __init__(self, model, chunk_seconds=6.):
        import sherpa_onnx as so
        cfg = so.VadModelConfig()
        cfg.silero_vad.model = str(model)
        cfg.silero_vad.min_silence_duration = .3
        cfg.silero_vad.min_speech_duration = .25
        cfg.silero_vad.max_speech_duration = chunk_seconds
        cfg.sample_rate = RATE
        cfg.num_threads = 1
        self.vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=chunk_seconds * 3)
        self.count = 0

    def feed(self, pcm):
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.
        self.count += len(samples)
        if len(samples) < FRAME:
            samples = np.pad(samples, (0, FRAME-len(samples)))
        self.vad.accept_waveform(samples)
        return self.drain()

    def drain(self):
        import numpy as np
        from bili_cli.asr.segment import Segment
        result = []
        while not self.vad.empty():
            seg = self.vad.front
            n = min(len(seg.samples), max(0, self.count-seg.start))
            if n >= RATE // 4:
                pcm = (np.clip(seg.samples[:n], -1, .999969) * 32768).astype('<i2').tobytes()
                result.append(Segment(seg.start/RATE, (seg.start+n)/RATE, pcm))
            self.vad.pop()
        return result

    def flush(self):
        self.vad.flush()
        return self.drain()


async def decoded_pcm(*, url=None, local_file=None):
    """Decode incrementally. HTTPX alone fetches remote data using existing proxy/TLS.

    No authenticated cookies are sent to CDN. Signed URL stays inside HTTPX and is
    never passed in ffmpeg argv or stderr. ffmpeg cannot open network protocols.
    """
    if (url is None) == (local_file is None):
        raise ValueError('Choose exactly one audio source')
    args = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1']
    if url is not None:
        args += ['-protocol_whitelist', 'pipe', '-i', 'pipe:0']
    else:
        args += ['-re', '-protocol_whitelist', 'file,pipe', '-i', str(local_file)]
    args += ['-vn', '-ac', '1', '-ar', str(RATE), '-f', 's16le', 'pipe:1']
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if url else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536)
    pump = None

    async def download():
        import httpx
        from bili_cli.runtime import WEB_HEADERS
        try:
            async with httpx.AsyncClient(headers=WEB_HEADERS, timeout=httpx.Timeout(15, read=15), follow_redirects=False) as client:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    async for block in response.aiter_bytes(16384):
                        proc.stdin.write(block)
                        await proc.stdin.drain()
        finally:
            proc.stdin.close()

    if url:
        pump = asyncio.create_task(download())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.readexactly(FRAME * 2), timeout=20)
            except asyncio.IncompleteReadError as exc:
                if len(exc.partial) >= 2:
                    yield exc.partial[:len(exc.partial)//2*2]
                break
            yield chunk
        if pump:
            await pump
        if await proc.wait() != 0:
            raise RuntimeError('Audio decoder failed')
    finally:
        if pump and not pump.done():
            pump.cancel()
        if pump:
            await asyncio.gather(pump, return_exceptions=True)
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


@dataclass
class Queued:
    segment: object
    session: int
    received_at: float
    digest: str


async def run_pipeline(source_factory, vad_factory, engine, *, duration=60., queue_size=3,
                       max_reconnects=2, max_age=20., emit=emit_json, reconnect_delay=1.):
    """Latest-audio policy drops oldest queued chunks, never grows without bound.

    source_factory(session) is an async iterator yielding <=512-sample PCM blocks.
    Each reconnect resets VAD and timestamps; exact recent PCM repeats are skipped.
    Cancellation closes producer and decoder, then waits for current bounded ASR.
    """
    if not 0 < duration <= 3600 or not 1 <= queue_size <= 16 or not 0 <= max_reconnects <= 10:
        raise ValueError('Invalid duration/queue/reconnect bounds')
    queue = asyncio.Queue(queue_size)
    recent = deque(maxlen=128)
    stats = dict(segments=0, dropped=0, duplicates=0, reconnects=0, audio_frames=0)
    started = time.monotonic()
    terminal = 'duration'

    def put(segment, session):
        digest = hashlib.sha256(segment.pcm).hexdigest()
        if any(h == digest and previous_session != session for h, previous_session in recent):
            stats['duplicates'] += 1
            return
        recent.append((digest, session))
        item = Queued(segment, session, time.monotonic(), digest)
        if queue.full():
            old = queue.get_nowait()
            queue.task_done()
            stats['dropped'] += 1
            emit(dict(event='gap', reason='backlog', session=old.session, start=old.segment.start, end=old.segment.end))
        queue.put_nowait(item)

    async def produce():
        nonlocal terminal
        for session in range(max_reconnects+1):
            vad = vad_factory()
            source = source_factory(session)
            emit(dict(event='connection', session=session))
            reason = 'stream_end'
            try:
                async for pcm in source:
                    if len(pcm) > FRAME*2 or len(pcm) % 2:
                        raise ValueError('PCM frame violates bound')
                    stats['audio_frames'] += len(pcm)//2
                    for seg in vad.feed(pcm):
                        put(seg, session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = 'stream_error'
                emit(dict(event='stream_error', session=session, error_type=type(exc).__name__))
            finally:
                if hasattr(source, 'aclose'):
                    await source.aclose()
                for seg in vad.flush():
                    put(seg, session)
            if session == max_reconnects:
                terminal = reason
                return
            stats['reconnects'] += 1
            emit(dict(event='reconnect', session=session+1, reason=reason))
            await asyncio.sleep(reconnect_delay)

    async def consume():
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                age = time.monotonic()-item.received_at
                if age > max_age:
                    stats['dropped'] += 1
                    emit(dict(event='gap', reason='stale', session=item.session, start=item.segment.start, end=item.segment.end))
                    continue
                t = time.monotonic()
                texts = await engine.transcribe([item.segment])
                elapsed = time.monotonic()-t
                stats['segments'] += 1
                emit(dict(event='transcript', session=item.session, start=item.segment.start, end=item.segment.end,
                          text=texts[0], recognition_seconds=round(elapsed,3), queue_wait_seconds=round(age,3),
                          observed_lag_seconds=round(time.monotonic()-item.received_at,3),
                          broadcast_latency_known=False, source_digest=item.digest[:16]))
            finally:
                queue.task_done()

    producer = asyncio.create_task(produce())
    consumer = asyncio.create_task(consume())
    cancelled = False
    producer_error = None
    try:
        done, _ = await asyncio.wait([producer, consumer], timeout=duration, return_when=asyncio.FIRST_COMPLETED)
        if consumer in done:
            await consumer  # surface decoder failure promptly
        if producer not in done:
            producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        if not producer.cancelled():
            producer_error = producer.exception()
    except asyncio.CancelledError:
        terminal = 'cancelled'
        cancelled = True
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
    finally:
        if not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        if cancelled or producer_error is not None:
            while not queue.empty():
                item = queue.get_nowait()
                queue.task_done()
                stats['dropped'] += 1
                emit(dict(event='gap', reason='cancelled' if cancelled else 'producer_error',
                          session=item.session, start=item.segment.start, end=item.segment.end))
        # Race sentinel insertion against consumer failure so a full queue cannot
        # deadlock shutdown. Never cancel an in-flight to_thread recognizer.
        if not consumer.done():
            finish = asyncio.create_task(queue.put(None))
            try:
                await asyncio.wait([finish, consumer], return_when=asyncio.FIRST_COMPLETED)
                if consumer.done():
                    await consumer
                else:
                    await asyncio.shield(consumer)
            finally:
                if not finish.done():
                    finish.cancel()
                await asyncio.gather(finish, return_exceptions=True)
        else:
            await consumer
    if producer_error is not None:
        raise producer_error
    result = dict(event='end', reason=terminal, elapsed_seconds=round(time.monotonic()-started,3), **stats)
    emit(result)
    if cancelled:
        raise asyncio.CancelledError()
    return result


async def transcribe(room_id=None, *, local_file=None, duration=60., chunk_seconds=6., queue_size=3,
                     max_reconnects=2, emit=emit_json):
    from bili_cli.asr import engines, settings
    if not 1 <= chunk_seconds <= 12:
        raise ValueError('chunk_seconds must be 1..12')
    if (room_id is None) == (local_file is None):
        raise ValueError('Supply exactly one room or local file')
    engine = engines.build('local')
    if not isinstance(engine, engines.SherpaEngine) or engine.model != 'qwen3-asr':
        raise RuntimeError('Existing local Qwen3-ASR CPU configuration required')
    engine.threads = min(engine.threads, 4)
    vad_model = settings.load().get('vad_model')
    if not vad_model or not Path(vad_model).is_file():
        raise RuntimeError('Existing local VAD model required')
    emit(dict(event='loading', engine=engine.id, chunk_seconds=chunk_seconds))
    engine._rec = await asyncio.to_thread(engine._build)
    emit(dict(event='ready', engine=engine.id, queue_size=queue_size, duration_seconds=duration,
              room_id=room_id, local_file=bool(local_file), broadcast_latency_known=False))

    async def source(session):
        if local_file:
            iterator = decoded_pcm(local_file=Path(local_file))
        else:
            from live_danmaku import get_stream_url
            url = await get_stream_url(room_id)
            iterator = decoded_pcm(url=url)
        try:
            async for pcm in iterator:
                yield pcm
        finally:
            await iterator.aclose()

    return await run_pipeline(source, lambda: StreamingVAD(vad_model, chunk_seconds), engine,
        duration=duration, queue_size=queue_size, max_reconnects=0 if local_file else max_reconnects, emit=emit)
