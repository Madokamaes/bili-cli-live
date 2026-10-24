import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speaker_diarization import spans_from_turns

def test_turn_partition():
    assert spans_from_turns([(0, 2, 4), (2, 4, 8)], 4) == [(0,2,['speaker_00']), (2,4,['speaker_01'])]

def test_overlap_not_false_assignment():
    assert spans_from_turns([(0, 3, 0), (2,4,1)], 4) == [(0,2,['speaker_00']), (2,3,['speaker_00','speaker_01']), (3,4,['speaker_01'])]

def test_clamp_split_order():
    spans = spans_from_turns([(-1, 41, 2)], 40)
    assert spans == [(0.,20.,['speaker_00']), (20.,40.,['speaker_00'])]

def test_invalid():
    try:
        spans_from_turns([(float('nan'),1,0)], 2)
        assert False
    except ValueError:
        pass

if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'): fn()
    print('4 partition tests passed')

# Network-free integration regressions, using mocked model outputs/engine.
import asyncio
import tempfile
import json
from unittest.mock import patch
from types import SimpleNamespace as NS
import numpy as np
import speaker_diarization as sd
from bili_cli.asr import engines
import sherpa_onnx as so

def test_mock_pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp); audio=d/'input.wav'; audio.write_bytes(b'input')
        model=d/'qwen'; (model/'tokenizer').mkdir(parents=True)
        (model/'x.onnx').write_bytes(b'model'); (model/'tokenizer/tokens').write_bytes(b'tokens')
        eng=engines.SherpaEngine('qwen3-asr',model)
        seen=[]
        async def transcribe(segments):
            seen.extend(segments)
            return ['hello'] * len(segments)
        eng.transcribe=transcribe
        turns=[NS(start=0.,end=.5,speaker=4),NS(start=.5,end=1.,speaker=8)]
        fake=NS(process=lambda x:NS(sort_by_start_time=lambda:turns))
        with patch('bili_cli.config.home',return_value=d), patch.object(sd,'SEG',audio), patch.object(sd,'EMB',audio), patch.object(sd,'normalize',return_value=np.arange(16000,dtype='<i2').tobytes()), patch.object(engines,'build',return_value=eng), patch.object(so.OfflineSpeakerDiarizationConfig,'validate',return_value=True), patch.object(so,'OfflineSpeakerDiarization',return_value=fake):
            a=asyncio.run(sd.transcribe_file(audio,offset=30))
            assert a['items'][0]['start']==30 and a['items'][-1]['end']==31
            assert len(seen[0].pcm)==16000 and seen[1].pcm[:2]==np.array([8000],dtype='<i2').tobytes()
            assert asyncio.run(sd.transcribe_file(audio,offset=30))['cached']
            assert not asyncio.run(sd.transcribe_file(audio,offset=31))['cached']
            (model/'tokenizer/tokens').write_bytes(b'changed')
            assert not asyncio.run(sd.transcribe_file(audio,offset=30))['cached']
            for cache in (d/'speaker-transcripts').glob('*.json'):
                cache.write_text('{broken')
            assert not asyncio.run(sd.transcribe_file(audio,offset=30))['cached']
            async def missing(segments): return []
            with patch.object(eng,'transcribe',side_effect=missing):
                try: asyncio.run(sd.transcribe_file(audio,offset=33))
                except RuntimeError: pass
                else: assert False
            with patch.object(sd.os,'replace',side_effect=OSError('test')):
                try: asyncio.run(sd.transcribe_file(audio,offset=32))
                except OSError: pass
                else: assert False
            assert not list((d/'speaker-transcripts').glob('*.tmp'))

def test_download_failure_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp)
        async def fail(playurl,key):
            p=d/'cache/audio'/f'{key}.part.wav';p.parent.mkdir(parents=True);p.write_bytes(b'partial')
            raise RuntimeError('test')
        with patch('bili_cli.config.home',return_value=d),patch('bili_cli.asr.audio.fetch_wav',side_effect=fail):
            try: asyncio.run(sd.transcribe_video_audio({},'key',-1,.5,True))
            except RuntimeError: pass
            else: assert False
        assert not list(d.rglob('*.wav'))

if __name__=='__main__':
    test_mock_pipeline(); test_download_failure_cleanup()
    print('Offset, PCM, cache, tokenizer invalidation and failure-cleanup tests passed')


def test_download_success_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp)
        async def fetch(playurl,key):
            p=d/'cache/audio'/f'{key}.wav';p.parent.mkdir(parents=True);p.write_bytes(b'audio')
            return p
        async def transcribe(path,*args):
            assert path.exists(); return {'ok':True}
        with patch('bili_cli.config.home',return_value=d), patch('bili_cli.asr.audio.fetch_wav',side_effect=fetch), patch.object(sd,'transcribe_file',side_effect=transcribe):
            assert asyncio.run(sd.transcribe_video_audio({},'BV_p1',-1,.5,True))=={'ok':True}
        assert not list(d.rglob('*.wav'))

if __name__=='__main__':
    test_download_success_cleanup()
    print('Download success cleanup passed')
