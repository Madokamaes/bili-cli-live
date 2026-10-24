"""Optional CPU diarization + existing Qwen ASR; no changes to original pipeline."""
from __future__ import annotations
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / 'diarization-models'
SEG = MODELS / 'sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx'
EMB = MODELS / 'embedding.onnx'
SCHEMA = 'speaker-transcript-v1'


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def spans_from_turns(turns, length, maximum=20.):
    """Partition at every turn boundary; mixed intervals are never attributed to one voice."""
    clean = []
    for start, end, speaker in turns:
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError('Nonfinite diarization timestamp')
        start, end = max(0., start), min(length, end)
        if end > start:
            clean.append((start, end, int(speaker)))
    labels = {}
    for _, _, speaker in sorted(clean):
        labels.setdefault(speaker, f'speaker_{len(labels):02d}')
    points = sorted(set(x for s, e, _ in clean for x in (s, e)))
    spans = []
    for start, end in zip(points, points[1:]):
        mid = (start + end) / 2
        active = sorted({labels[k] for s, e, k in clean if s <= mid < e})
        if not active:
            continue
        if spans and spans[-1][1] == start and spans[-1][2] == active:
            spans[-1] = (spans[-1][0], end, active)
        else:
            spans.append((start, end, active))
    out = []
    for start, end, active in spans:
        count = max(1, math.ceil((end - start) / maximum))
        for i in range(count):
            out.append((start + (end-start)*i/count, start + (end-start)*(i+1)/count, active))
    return out


def normalize(path):
    return subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', str(path),
                           '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', '-'],
                          check=True, capture_output=True).stdout


async def transcribe_file(path, speakers=-1, threshold=.5, offset=0., use_cache=True, source_key=None):
    from bili_cli import config
    from bili_cli.asr import engines
    from bili_cli.asr.segment import Segment
    import numpy as np
    import sherpa_onnx as so
    if speakers != -1 and speakers < 1:
        raise ValueError('speakers must be -1 (unknown) or positive')
    if not math.isfinite(offset) or offset < 0 or not math.isfinite(threshold) or not 0 < threshold < 2:
        raise ValueError('Invalid offset or threshold')
    if importlib.metadata.version('sherpa-onnx') != '2.0.0':
        raise RuntimeError('Diarization requires reviewed sherpa-onnx 2.0.0')
    eng = engines.build('local')
    if not isinstance(eng, engines.SherpaEngine) or eng.model != 'qwen3-asr':
        raise RuntimeError('This integration is verified only for CPU Qwen3-ASR')
    eng.threads = min(4, eng.threads)
    identity = dict(schema=SCHEMA, source_sha256=sha(path), source_key=source_key,
                    offset=offset, speakers=speakers, threshold=threshold,
                    segmentation_sha256=sha(SEG), embedding_sha256=sha(EMB),
                    implementation_sha256=sha(__file__), sherpa_version=importlib.metadata.version('sherpa-onnx'),
                    threads=eng.threads, min_duration_on=.3, min_duration_off=.5, maximum_segment=20.,
                    asr_engine=eng.id, asr_models={str(p.relative_to(eng.model_dir)): sha(p)
                    for p in eng.model_dir.rglob('*') if p.is_file()})
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache_dir = config.home() / 'speaker-transcripts'
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / (key + '.json')
    if use_cache and cache.exists():
        try:
            saved = json.loads(cache.read_text())
            if saved.get('schema') == SCHEMA and saved.get('identity') == identity and isinstance(saved.get('items'), list):
                return {**saved, 'cached': True}
        except (OSError, ValueError, AttributeError):
            pass
    started = time.perf_counter()
    cpu = time.process_time()
    pcm = normalize(path)
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.
    duration = len(samples) / 16000
    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG)),
            num_threads=4, provider='cpu'),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(EMB), num_threads=4, provider='cpu'),
        clustering=so.FastClusteringConfig(num_clusters=speakers, threshold=threshold),
        min_duration_on=.3, min_duration_off=.5)
    if not cfg.validate():
        raise RuntimeError('Invalid diarization model configuration')
    diarizer = so.OfflineSpeakerDiarization(cfg)
    result = await asyncio.to_thread(diarizer.process, samples)
    turns = [(float(r.start), float(r.end), int(r.speaker)) for r in result.sort_by_start_time()]
    del result, diarizer
    diarization_seconds = time.perf_counter() - started
    spans = spans_from_turns(turns, duration)
    segments, metadata = [], []
    for start, end, active in spans:
        # Quantize both timestamps and slices to the exact same 16 kHz sample grid.
        a, b = max(0, round(start * 16000)), min(len(samples), round(end * 16000))
        if b <= a:
            continue
        segments.append(Segment(a/16000, b/16000, pcm[a*2:b*2]))
        metadata.append(active)
    # Serial, existing recognizer; no hotwords or cloud uploads.
    texts = await eng.transcribe(segments)
    if len(texts) != len(segments):
        raise RuntimeError('ASR returned a different segment count; refusing truncated output')
    items = []
    for seg, active, text in zip(segments, metadata, texts):
        mixed = len(active) > 1
        items.append(dict(start=round(offset+seg.start, 6), end=round(offset+seg.end, 6),
                          speaker='mixed' if mixed else active[0], speakers=active, overlap=mixed,
                          text=text, attribution='uncertain_overlap' if mixed else 'model_estimate'))
    data = dict(schema=SCHEMA, engine=eng.id, timestamp_unit='seconds',
                timestamp_basis='source_global' if offset else 'input_audio', source_offset_seconds=offset,
                duration_seconds=duration, identity=identity, items=items,
                limitations=['Segment boundaries, not word alignment.',
                             'Speaker labels are file-local clusters, not identities.',
                             'Overlap detection is not guaranteed; mixed speech is not source-separated.'],
                metrics=dict(diarization_seconds=round(diarization_seconds,3),
                             elapsed_seconds=round(time.perf_counter()-started,3),
                             cpu_seconds=round(time.process_time()-cpu,3),
                             process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cache)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {**data, 'cached': False}


async def transcribe_video_audio(playurl, key, speakers, threshold, use_cache):
    from bili_cli import config
    from bili_cli.asr.audio import fetch_wav
    # Unique owned download path: never remove a shared/default pipeline audio cache.
    temp_key = 'diarize-' + uuid.uuid4().hex
    expected = config.home() / 'cache' / 'audio' / (temp_key + '.wav')
    try:
        wav = await fetch_wav(playurl, temp_key)
        return await transcribe_file(wav, speakers, threshold, 0., use_cache, key)
    finally:
        expected.unlink(missing_ok=True)
        expected.with_suffix('.part.wav').unlink(missing_ok=True)


def register():
    import typer
    from bili_cli.commands import asr, video
    from bili_cli.runtime import run
    if importlib.metadata.version('bili-agent') != '0.7.0':
        raise RuntimeError('Review diarization registration for changed bili-agent version')

    @asr.app.command('diarize')
    def diarize(audio: Path = typer.Argument(..., exists=True, dir_okay=False),
                speakers: int = typer.Option(-1, '--speakers'),
                threshold: float = typer.Option(.5, '--threshold'),
                offset: float = typer.Option(0., '--offset', min=0.),
                no_cache: bool = typer.Option(False, '--no-cache')):
        """Local audio -> speaker-labelled Qwen transcript. --offset maps a known clip to original video seconds."""
        async def body():
            return await transcribe_file(audio, speakers, threshold, offset, not no_cache)
        run(body)

    @video.app.command('transcribe-speakers')
    def transcribe_speakers(video_id: str = video.VIDEO_ARG, page: int = video.PAGE_OPT,
                            speakers: int = typer.Option(-1, '--speakers'),
                            threshold: float = typer.Option(.5, '--threshold'),
                            max_minutes: float = typer.Option(60., '--max-minutes'),
                            no_cache: bool = typer.Option(False, '--no-cache')):
        """Optional diarization + CPU Qwen, separate from the original transcript cache."""
        async def body():
            from bili_cli.asr.audio import fetch_wav
            from bili_cli.errors import BiliError
            if (speakers != -1 and speakers < 1) or not math.isfinite(threshold) or not 0 < threshold < 2 or not math.isfinite(max_minutes) or max_minutes <= 0:
                raise BiliError('invalid_argument', 'Invalid speakers, threshold, or max-minutes')
            v = video._video(video_id)
            info = await v.get_info()
            pg = video._pick_page(info, page)
            if (pg.get('duration') or 0) > max_minutes * 60:
                raise BiliError('invalid_argument', 'Video exceeds --max-minutes')
            key = f"{info['bvid']}_p{page}"
            result = await transcribe_video_audio(await v.get_download_url(cid=pg['cid']),
                                                  key, speakers, threshold, not no_cache)
            return {**result, 'bvid':info['bvid'], 'page':page, 'timestamp_basis':'video_page_global'}
        run(body)
