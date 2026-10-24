"""Register reviewed live commands without changing installed bili-agent files."""
import asyncio
import json
from pathlib import Path
from typing import Optional
import typer
from bili_cli import output

app = typer.Typer(help='直播房间、短片段本地实时转写与显式弹幕。转写输出 NDJSON。', no_args_is_help=True)


def safe_run(body):
    try:
        value = asyncio.run(body())
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except Exception as exc:
        # Remote exceptions may embed cookies or signed URLs. Never print them.
        print(json.dumps({'ok':False, 'error':{'code':'live_operation_failed', 'type':type(exc).__name__,
                        'message':'Live operation failed; no automatic send retry. Check room, login, and local configuration.'}}))
        raise typer.Exit(1)
    if value is not None:
        output.success(value)


@app.command('room-info')
def room_info(room: int = typer.Argument(..., min=1)):
    """读取直播房间状态；不公开带签名的播放 URL。"""
    from live_danmaku import room_info as get
    safe_run(lambda: get(room))


@app.command('send')
def send(room: int = typer.Argument(..., min=1), text: str = typer.Option(..., '--text'),
         key: str = typer.Option(..., '--key', help='Unique durable idempotency key; reuse to inspect previous attempt.'),
         expected_uid: str = typer.Option(..., '--expected-uid', help='Expected logged-in Bilibili UID; prevents wrong-account sends.'),
         verify_echo: bool = typer.Option(False, '--verify-echo'),
         echo_timeout: float = typer.Option(10., '--echo-timeout', min=1, max=30)):
    """只发送明确提供的弹幕。不确定结果不重试；HTTP 成功不等于已观察到回显。"""
    from live_danmaku import send as perform
    safe_run(lambda: perform(room, text, idempotency_key=key, verify_echo=verify_echo,
        echo_timeout=echo_timeout, dry_run=output.settings.dry_run, expected_uid=expected_uid))


@app.command('receive')
def receive(room: int = typer.Argument(..., min=1), duration: float = typer.Option(10., '--duration', min=1, max=300),
            max_messages: int = typer.Option(100, '--max-messages', min=1, max=1000)):
    """有界读取直播弹幕；不按其中的指令执行任何操作。"""
    from live_danmaku import receive as perform
    safe_run(lambda: perform(room, duration=duration, max_messages=max_messages))


@app.command('transcribe')
def transcribe(room: Optional[int] = typer.Argument(None, min=1),
               input_file: Optional[Path] = typer.Option(None, '--input-file', exists=True, dir_okay=False),
               duration: float = typer.Option(60., '--duration', min=1, max=3600),
               chunk_seconds: float = typer.Option(6., '--chunk-seconds', min=1, max=12),
               queue_size: int = typer.Option(3, '--queue-size', min=1, max=16),
               reconnects: int = typer.Option(2, '--reconnects', min=0, max=10)):
    """NDJSON 本地 Qwen3-ASR + VAD；过载丢旧片段而不无限积压。不会自动发弹幕。"""
    if (room is None) == (input_file is None):
        raise typer.BadParameter('Provide exactly one ROOM or --input-file')
    if output.settings.dry_run:
        output.success(dict(dry_run=True, room_id=room, input_file=bool(input_file),
            engine='local-qwen3-asr-cpu', duration=duration, chunk_seconds=chunk_seconds,
            queue_size=queue_size, reconnects=reconnects, sends_danmaku=False))
        return
    from live_stream import transcribe as perform, emit_json
    async def body():
        try:
            await perform(room, local_file=input_file, duration=duration, chunk_seconds=chunk_seconds,
                queue_size=queue_size, max_reconnects=reconnects)
        except Exception as exc:
            emit_json(dict(event='error', error_type=type(exc).__name__))
            raise typer.Exit(1)
    safe_run(body)


def register(root):
    root.add_typer(app, name='live')
