"""Explicit, one-shot live danmaku operations for bilibili-api-python 17.4.2.

No network, credential reads, transport changes or monkey patches on import.
A successful HTTP/business response is NOT evidence that viewers saw a message.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
import time
from importlib.metadata import version
from typing import Any
from urllib.parse import urlsplit
import zlib

from bili_cli import config, guard
from bili_cli.errors import BiliError
from bilibili_api import live
from bilibili_api.utils import network
from bili_safe_diagnostics import summarize_response

SDK_VERSION = "17.4.2"
SEND_URL = "https://api.live.bilibili.com/msg/send"
MAX_TEXT_LENGTH = 100
LIVE_MIN_INTERVAL = 10
LIVE_PER_HOUR = 100
MODES = {"scroll": 1, "top": 5, "bottom": 4}
_SUCCESS_MESSAGES = {None, "", "0", "ok", "OK", "success", "成功"}


def _sdk_guard() -> None:
    if version("bilibili-api-python") != SDK_VERSION:
        raise BiliError("internal_error", "直播扩展 SDK 版本已变化，需复核后再运行。")


def _room_id(value: int | str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,17}", str(value)):
        raise BiliError("invalid_argument", "room_id 必须是明确的正整数直播间号。")
    return int(value)


def _bounded(value: float, minimum: float, maximum: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise BiliError("invalid_argument", f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return float(value)


def _fingerprint(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ScopedTestSession:
    """Explicit, expiring local-only interval exception for at most five writes.

    Does not alter the default policy or server limits. A session is pinned to
    one account, canonical room, exact text and predetermined idempotency keys.
    """
    def __init__(self, *, room_id, expected_uid, text, key_prefix, expires_at,
                 count=5, interval=1.0, allow_offline=False):
        self.room_id = _room_id(room_id)
        self.expected_uid = str(expected_uid)
        self.text = text
        if not isinstance(key_prefix, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", key_prefix):
            raise BiliError("invalid_argument", "Invalid test session prefix")
        if type(count) is not int or not 1 <= count <= 5:
            raise BiliError("invalid_argument", "Test session maximum is five")
        self.interval = _bounded(interval, 1, 10, "test interval")
        self.expires_at = _bounded(expires_at, time.time(), time.time() + 1800, "test expiry")
        self.keys = {f"{key_prefix}-{n}" for n in range(1, count + 1)}
        self.session_id = hashlib.sha256(key_prefix.encode()).hexdigest()
        self.count = count
        self.allow_offline = allow_offline is True
        self.credential = None
        self.metadata = None
        self.stopped = False
        self.busy = False

    def validate(self, room_id, text, key, expected_uid, allow_offline):
        from bili_cli import output
        if guard._disabled() or output.settings.wait:
            raise BiliError("invalid_argument", "Test requires enabled guard and disabled automatic waiting")
        if (self.stopped or time.time() >= self.expires_at or room_id != self.room_id
                or text != self.text or key not in self.keys
                or str(expected_uid) != self.expected_uid
                or allow_offline is not self.allow_offline):
            raise BiliError("invalid_argument", "Test session scope or expiry mismatch")

    async def send(self, key):
        if self.busy:
            raise BiliError("invalid_argument", "Concurrent test submissions forbidden")
        self.busy = True
        try:
            result = await send(self.room_id, self.text, idempotency_key=key,
                                expected_uid=self.expected_uid, allow_offline=self.allow_offline,
                                _test_session=self)
            if result.get("status") != "accepted" or result.get("idempotent_replay"):
                self.stopped = True
            return result
        except BaseException:
            self.stopped = True
            raise
        finally:
            self.busy = False


class _Ledger:
    """Fail-closed, process-safe, durable intent journal; never stores cookies."""
    def __init__(self, directory: Path | None = None):
        self.directory = directory or config.home()
        self.path = self.directory / "live_danmaku_ledger.json"
        self.lock_path = self.directory / "live_danmaku_ledger.lock"

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BiliError("rate_limited", "直播发送记录正忙；没有发送，请稍后核对。") from None
            if self.path.exists():
                try:
                    entries = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(entries, dict) or any(not isinstance(v, dict) for v in entries.values()):
                        raise ValueError("invalid ledger")
                except (OSError, ValueError):
                    raise BiliError("internal_error", "直播发送记录无法安全读取；已停止发送，请先人工核对。") from None
            else:
                entries = {}
            yield entries
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def write(self, entries: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".live_danmaku_", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    def existing(self, key: str, fingerprint: str) -> dict | None:
        with self.locked() as entries:
            return self._existing(entries, key, fingerprint)

    @staticmethod
    def _existing(entries: dict, key: str, fingerprint: str) -> dict | None:
        entry = entries.get(key)
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise BiliError("invalid_argument", "此幂等键已经用于不同账号、直播间或弹幕内容；没有发送。")
        if not isinstance(entry.get("result"), dict):
            raise BiliError("internal_error", "直播发送记录不完整；没有发送，请先人工核对。")
        result = {**entry["result"], "idempotent_replay": True}
        if result.get("status") == "in_flight":
            result.update(status="uncertain", business_success=None, echo_observed=False,
                          reason="prior_attempt_in_flight_or_interrupted", retry_allowed=False)
        return result

    def claim(self, key: str, fingerprint: str, result: dict, test_session=None) -> dict | None:
        with self.locked() as entries:
            existing = self._existing(entries, key, fingerprint)
            if existing is not None:
                return existing
            # Guard's state is shared with video danmaku; journal also counts
            # uncertain live attempts, so a crash cannot waive the local limit.
            now = time.time()
            recent = [e.get("started_at", 0) for e in entries.values()
                      if isinstance(e.get("started_at"), (int, float)) and now - e["started_at"] < 3600]
            interval = test_session.interval if test_session else LIVE_MIN_INTERVAL
            if test_session:
                session_entries = [e for e in entries.values() if e.get("test_session") == test_session.session_id]
                if (now >= test_session.expires_at or len(session_entries) >= test_session.count
                        or any(e.get("result", {}).get("status") != "accepted" for e in session_entries)):
                    raise BiliError("rate_limited", "Test session exhausted, expired or stopped")
            if recent and (now - max(recent) < interval or len(recent) >= LIVE_PER_HOUR):
                raise BiliError("rate_limited", f"直播弹幕发送间隔至少 {LIVE_MIN_INTERVAL} 秒，每小时最多 {LIVE_PER_HOUR} 次，含结果未知的尝试。")
            # Synchronous, narrowly scoped injection into the existing shared
            # policy gate; no await and no persistent/global configuration edit.
            original_limit = guard.LIMITS["danmaku"]
            try:
                guard.LIMITS["danmaku"] = (interval, LIVE_PER_HOUR)
                guard.check("danmaku")
            finally:
                guard.LIMITS["danmaku"] = original_limit
            entries[key] = {"fingerprint": fingerprint, "started_at": now, "result": result}
            if test_session:
                entries[key]["test_session"] = test_session.session_id
            self.write(entries)
            return None

    def finish(self, key: str, result: dict) -> None:
        with self.locked() as entries:
            if key not in entries:
                raise BiliError("internal_error", "直播发送记录丢失；请勿重新发送。")
            entries[key]["result"] = result
            entries[key]["finished_at"] = time.time()
            self.write(entries)


async def room_info(room_id: int | str) -> dict:
    """Read public room metadata, omitting playback URLs and access tokens."""
    _sdk_guard()
    requested = _room_id(room_id)
    room = live.LiveRoom(requested, credential=config.optional_credential())
    play = await room.get_room_play_info()
    canonical = _room_id(play.get("room_id"))
    details = await live.LiveRoom(canonical, credential=room.credential).get_room_info()
    info = details.get("room_info") or {}
    anchor = (details.get("anchor_info") or {}).get("base_info") or {}
    return {"requested_room_id": requested, "room_id": canonical,
            "uid": play.get("uid"), "title": info.get("title"),
            "uname": anchor.get("uname"), "live_status": play.get("live_status"),
            "is_locked": bool(play.get("is_locked", False)),
            "url": f"https://live.bilibili.com/{canonical}"}


async def get_stream_url(room_id: int | str) -> str:
    """Internal capture input only. Never print, log or persist this signed URL."""
    _sdk_guard()
    room = live.LiveRoom(_room_id(room_id), credential=config.optional_credential())
    data = await room.get_room_play_info_v2(live_protocol=live.LiveProtocol.FLV,
                                          live_format=live.LiveFormat.FLV,
                                          live_codec=live.LiveCodec.AVC)
    if data.get("live_status") != 1:
        raise BiliError("not_found", "直播间当前未开播。")
    streams = ((data.get("playurl_info") or {}).get("playurl") or {}).get("stream") or []
    for stream in streams:
        if stream.get("protocol_name") != "http_stream":
            continue
        for fmt in stream.get("format") or []:
            if fmt.get("format_name") != "flv":
                continue
            for codec in fmt.get("codec") or []:
                if codec.get("codec_name") != "avc":
                    continue
                for source in codec.get("url_info") or []:
                    url = str(source.get("host", "")) + str(codec.get("base_url", "")) + str(source.get("extra", ""))
                    parsed = urlsplit(url)
                    if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                        return url
    raise BiliError("not_found", "没有可用的 HTTPS FLV/AVC 直播流。")


def _classify(payload: Any, http_status: int) -> dict:
    result = {"http_status": http_status, "business_success": None, "status": "uncertain",
              "retry_allowed": False, "diagnostic": summarize_response(http_status, payload)}
    if http_status != 200 or not isinstance(payload, dict) or type(payload.get("code")) is not int:
        result["reason"] = "unconfirmed_response"
        return result
    result["business_code"] = payload["code"]
    if payload["code"] != 0:
        result.update(status="rejected", business_success=False, reason="business_rejection")
        return result
    # Some live endpoints return code=0 AND a rejection in top-level msg.
    # Unknown prose is not accepted as success and never logged verbatim.
    data = payload.get("data")
    messages = [payload.get("msg"), payload.get("message")]
    if isinstance(data, dict):
        messages += [data.get("msg"), data.get("message")]
    if any(not isinstance(value, (str, type(None))) or value not in _SUCCESS_MESSAGES for value in messages):
        result["reason"] = "nonempty_or_unrecognized_business_message"
        return result
    result.update(status="accepted", business_success=True, reason="business_acknowledged")
    return result


async def send(room_id: int | str, text: str, *, idempotency_key: str,
               verify_echo: bool = False, echo_timeout: float = 10.0,
               dry_run: bool = False, color: str = "ffffff", mode: str = "scroll",
               expected_uid: str | int | None = None, allow_offline: bool = False,
               _test_session: ScopedTestSession | None = None) -> dict:
    """Submit exactly one explicitly supplied message, never retrying a write.

    Reusing an idempotency key returns the persisted result without sending.
    Interrupted/ambiguous outcomes remain uncertain, including across processes.
    Echo collection, if requested, starts BEFORE the single POST and only counts
    a subsequent event matching sender UID, room and exact text.
    """
    _sdk_guard()
    requested = _room_id(room_id)
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise BiliError("invalid_argument", f"弹幕需为 1 到 {MAX_TEXT_LENGTH} 字，且不含换行或控制字符。")
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise BiliError("invalid_argument", "必须提供 1 到 128 位明确幂等键，仅限字母、数字、点、下划线、冒号或连字符。")
    if mode not in MODES or not isinstance(color, str) or not re.fullmatch(r"#?[0-9A-Fa-f]{6}", color):
        raise BiliError("invalid_argument", "弹幕模式或颜色无效。")
    timeout = _bounded(echo_timeout, 0.1, 60, "echo_timeout")
    color = color.lstrip("#").lower()
    # Dry runs are fully offline and do not read credentials or write a ledger.
    if dry_run:
        return {"dry_run": True, "action": "live.send", "requested_room_id": requested,
                "text": text, "color": color, "mode": mode, "idempotency_key": idempotency_key,
                "business_success": None, "echo_observed": False, "status": "not_sent"}
    if _test_session:
        _test_session.validate(requested, text, idempotency_key, expected_uid, allow_offline)
    cred = (_test_session.credential if _test_session else None) or config.require_credential()
    cred.raise_for_no_sessdata()
    cred.raise_for_no_bili_jct()
    cred.raise_for_no_dedeuserid()
    if cred.proxy:
        raise BiliError("invalid_argument", "直播单次发送不支持凭据专属代理；已停止，避免静默改变网络路径。")
    if expected_uid is not None and str(cred.dedeuserid) != str(expected_uid):
        raise BiliError("invalid_argument", "当前登录账号不是明确指定的账号；没有发送。")
    params = {"requested_room_id": requested, "text": text, "color": color, "mode": mode,
              "sender_uid": str(cred.dedeuserid)}
    fingerprint = _fingerprint(params)
    key = hashlib.sha256(idempotency_key.encode()).hexdigest()
    ledger = _Ledger()
    previous = ledger.existing(key, fingerprint)
    if previous is not None:
        return previous
    room = live.LiveRoom(requested, credential=cred)
    metadata = (_test_session.metadata if _test_session else None) or await room.get_room_play_info()
    canonical = _room_id(metadata.get("room_id"))
    if metadata.get("is_locked") or (metadata.get("live_status") != 1 and not (allow_offline is True and metadata.get("live_status") == 0)):
        raise BiliError("invalid_argument", "直播间未开播或已锁定；没有发送。")
    if _test_session:
        if canonical != _test_session.room_id:
            raise BiliError("invalid_argument", "Test canonical room mismatch")
        _test_session.credential = cred
        _test_session.metadata = metadata
    api = network.Api(**live.API["operate"]["send_danmaku"], credential=cred).update_data(
        mode=MODES[mode], msg=text, roomid=canonical, bubble=0, rnd=int(time.time()),
        color=int(color, 16), fontsize=25)
    if api.url != SEND_URL or api.method != "POST":
        raise BiliError("internal_error", "直播发送端点已变化；没有发送。")
    # SDK preparation adds CSRF/cookies; it does not submit this write. Use the
    # selected transport directly below so Api retry machinery is never entered.
    request = await api._prepare_request()
    request["allow_redirects"] = False
    client = network.get_client()
    observer = _Observer(canonical, cred) if verify_echo else None
    echo_setup = None
    if observer:
        try:
            await observer.start(timeout=min(timeout, 10.0))
        except BaseException as exc:
            await observer.close()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            echo_setup = "unavailable"
            observer = None
    result = {"requested_room_id": requested, "room_id": canonical,
              "status": "in_flight", "business_success": None, "echo_observed": False,
              "echo_status": "listening" if observer else (echo_setup or "not_requested"),
              "idempotent_replay": False, "retry_allowed": False,
              "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    try:
        previous = ledger.claim(key, fingerprint, result, _test_session)
        if previous is not None:
            return previous
        submitted_at = time.time()
        result["submitted_at"] = submitted_at
        result["submitted_monotonic"] = time.monotonic()
        if observer:
            observer.arm(submitted_at, str(cred.dedeuserid), text)
        try:
            # The ONLY outbound message submission. No retry loop or redirect.
            response = await client.request(**request)
            try:
                payload = json.loads(response.utf8_text())
            except (ValueError, UnicodeError):
                payload = None
            result.update(_classify(payload, response.code))
        except asyncio.CancelledError:
            result.update(status="uncertain", reason="cancelled_during_submission")
            ledger.finish(key, result)
            raise
        except Exception as exc:
            result.update(status="uncertain", reason="submission_exception", exception_type=type(exc).__name__)
        # Persist transport outcome before awaiting optional observation.
        ledger.finish(key, result)
        if observer:
            result["echo_observed"] = await observer.wait_echo(timeout)
            result["echo_status"] = "observed" if result["echo_observed"] else ("unavailable" if observer.error else "not_observed")
            if result["echo_observed"]:
                result["echo_evidence"] = "sender_room_text_and_timestamp_correlation_not_message_id_proof"
            ledger.finish(key, result)
        if result["business_success"] is True:
            try:
                guard.record("danmaku", {"room_id": canonical, "text_sha256": result["text_sha256"]},
                             {"live": True, "business_success": True, "echo_observed": result["echo_observed"]})
            except Exception:
                result["audit_warning"] = "shared_audit_write_failed; durable_live_ledger_retained"
        return result
    finally:
        if observer:
            await observer.close()


def _packet(body: bytes, operation: int) -> bytes:
    return live.LiveDanmaku._LiveDanmaku__pack(body, 1, operation)


def _events(data: bytes, depth: int = 0, _budget: dict | None = None) -> list[tuple[int, Any]]:
    """Parse frames with one aggregate byte/event budget across all recursion."""
    budget = _budget if _budget is not None else {"bytes": 4 * 1024 * 1024, "events": 4096}
    if depth > 3 or len(data) > budget["bytes"]:
        raise ValueError("live frame limit")
    budget["bytes"] -= len(data)
    events = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 16:
            raise ValueError("truncated live frame")
        size, header, protocol, operation, _ = struct.unpack(">IHHII", data[offset:offset + 16])
        if header < 16 or size < header or offset + size > len(data):
            raise ValueError("invalid live frame length")
        body = data[offset + header:offset + size]
        if protocol == 2:
            decompressor = zlib.decompressobj()
            unpacked = decompressor.decompress(body, budget["bytes"] + 1)
            if decompressor.unconsumed_tail or not decompressor.eof or len(unpacked) > budget["bytes"]:
                raise ValueError("compressed live frame limit")
            events.extend(_events(unpacked, depth + 1, budget))
        elif protocol in (0, 1) and operation == 3:
            if len(body) != 4:
                raise ValueError("invalid heartbeat response")
            budget["events"] -= 1
            if budget["events"] < 0:
                raise ValueError("live event limit")
            events.append((3, struct.unpack(">I", body)[0]))
        elif protocol in (0, 1) and operation in (5, 8):
            budget["events"] -= 1
            if budget["events"] < 0:
                raise ValueError("live event limit")
            events.append((operation, json.loads(body)))
        elif protocol not in (0, 1):
            raise ValueError("unsupported live protocol")
        offset += size
    return events


def _message(payload: Any, room_id: int) -> dict | None:
    if not isinstance(payload, dict) or str(payload.get("cmd", "")).split(":", 1)[0] != "DANMU_MSG":
        return None
    info = payload.get("info")
    if not isinstance(info, list) or len(info) < 3 or not isinstance(info[1], str) or len(info[1]) > 4096 or not isinstance(info[2], list) or not info[2]:
        return None
    uid = info[2][0]
    if type(uid) not in (str, int) or len(str(uid)) > 20 or not str(uid).isdigit():
        return None
    timestamp = None
    if isinstance(info[0], list) and len(info[0]) > 4 and isinstance(info[0][4], (int, float)):
        timestamp = info[0][4]
        if timestamp > 10**11:
            timestamp /= 1000
    return {"room_id": room_id, "uid": str(uid), "text": info[1],
            "server_timestamp": timestamp, "received_at": time.time()}


class _Observer:
    """Read-only live socket using a private curl session, never global selection."""
    def __init__(self, room_id: int, credential):
        self.room_id = room_id
        self.credential = credential
        self.session = None
        self.ws = None
        self.task = None
        self.heartbeat = None
        self.ready = asyncio.Event()
        self.echo = asyncio.Event()
        self.messages = []
        self.match = None
        self.error = None
        self.max_messages = 100
        self.last_heartbeat_ack = None

    async def start(self, timeout: float = 10.0):
        from curl_cffi.requests import AsyncSession
        async with asyncio.timeout(timeout):
            room = live.LiveRoom(self.room_id, credential=self.credential)
            info = await room.get_danmu_info()
            hosts = info.get("host_list") or []
            if not hosts or not isinstance(info.get("token"), str):
                raise ValueError("missing live socket configuration")
            host = hosts[0]
            name, port = host.get("host", ""), host.get("wss_port")
            if not re.fullmatch(r"[A-Za-z0-9.-]+\.bilibili\.com", name) or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("untrusted live socket target")
            buvid = self.credential.buvid3
            if not buvid:
                buvid = (await network.get_buvid())[0]
            # TLS verification and environment proxy remain enabled/default.
            proxy = self.credential.proxy or network.request_settings.get_proxy()
            options = {"proxy": proxy} if proxy else {}
            self.session = AsyncSession(timeout=timeout, trust_env=True, verify=True, **options)
            self.ws = await self.session.ws_connect(f"wss://{name}:{port}/sub", headers=network.HEADERS.copy())
            auth = {"uid": int(self.credential.dedeuserid or 0), "roomid": self.room_id,
                    "protover": 2, "platform": "web", "type": 2, "buvid": buvid, "key": info["token"]}
            await self.ws.send_binary(_packet(json.dumps(auth, separators=(",", ":")).encode(), 7))
            self.task = asyncio.create_task(self._read())
            ready = asyncio.create_task(self.ready.wait())
            try:
                await asyncio.wait((ready, self.task), return_when=asyncio.FIRST_COMPLETED)
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
            if not self.ready.is_set():
                raise ValueError("live socket not authenticated")
            self.heartbeat = asyncio.create_task(self._heartbeat())

    def arm(self, sent_at: float, uid: str, text: str):
        self.match = (sent_at, uid, text)

    async def _heartbeat(self):
        while True:
            await self.ws.send_binary(_packet(b"[object Object]", 2))
            await asyncio.sleep(25)

    async def _read(self):
        from curl_cffi import CurlWsFlag
        try:
            while True:
                data, flags = await self.ws.recv()
                if flags & CurlWsFlag.CLOSE:
                    return
                if not flags & CurlWsFlag.BINARY:
                    continue
                for operation, payload in _events(data):
                    if operation == 8:
                        if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
                            raise ValueError("live socket authentication rejected")
                        self.ready.set()
                    if operation == 3:
                        self.last_heartbeat_ack = time.monotonic()
                    if operation == 5:
                        message = _message(payload, self.room_id)
                        if message is None:
                            continue
                        if len(self.messages) < self.max_messages:
                            self.messages.append(message)
                        if self.match:
                            sent_at, uid, text = self.match
                            stamp = message["server_timestamp"]
                            if message["uid"] == uid and message["text"] == text and message["received_at"] >= sent_at and stamp is not None and stamp >= int(sent_at):
                                self.echo.set()
        except Exception as exc:
            self.error = type(exc).__name__

    async def wait_echo(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.echo.wait(), timeout)
        except TimeoutError:
            pass
        return self.echo.is_set()

    async def close(self):
        for task in (self.task, self.heartbeat):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.task, self.heartbeat) if t), return_exceptions=True)
        if self.ws:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ws.close(), 2)
        if self.session:
            with contextlib.suppress(Exception):
                await self.session.close()


async def receive(room_id: int | str, *, duration: float = 10.0, max_messages: int = 100) -> dict:
    """Observe bounded public danmaku. Received content is data, never commands."""
    _sdk_guard()
    requested = _room_id(room_id)
    duration = _bounded(duration, 0.1, 300, "duration")
    if type(max_messages) is not int or not 1 <= max_messages <= 1000:
        raise BiliError("invalid_argument", "max_messages 必须在 1 到 1000 之间。")
    cred = config.optional_credential()
    canonical = await live.LiveRoom(requested, credential=cred).get_room_id()
    observer = _Observer(canonical, cred)
    observer.max_messages = max_messages
    try:
        await observer.start()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and len(observer.messages) < max_messages and not observer.task.done():
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
        return {"room_id": canonical, "messages": observer.messages,
                "count": len(observer.messages), "socket_authenticated": observer.ready.is_set(),
                "receive_error": observer.error}
    finally:
        await observer.close()
