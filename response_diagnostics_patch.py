"""Guarded CLI error-observability patch. No request/retry/identity changes."""
import json
import functools
import hashlib
import inspect
import sys
from importlib.metadata import version
from bili_safe_diagnostics import summarize_response, safe_text, shape

EXPECTED = {'bili-agent':'0.7.0', 'bilibili-api-python':'17.4.2'}
HASHES = {'process':'ea0a362e5b20138fc3bae974c6883c35b861920e0a5c2189823980de588c3b63',
          'error':'5941e492c4b6bdf54670078f3e22512dd7204e2f275b6622ec386c9b188f45d1'}

def apply():
    from bilibili_api.utils.network import Api
    from bilibili_api.exceptions import ResponseCodeException, NetworkException
    from bili_cli import errors
    original_process, original_error = Api._process_response, errors.from_exception
    if getattr(original_error, '_dot_safe_diagnostics', False): return
    if 'bili_cli.runtime' in sys.modules:
        raise RuntimeError('Diagnostic patch must load before bili_cli.runtime')
    for name, expected in EXPECTED.items():
        if version(name) != expected: raise RuntimeError('Diagnostic patch version mismatch; review required')
    for name, fn in [('process', original_process), ('error', original_error)]:
        if hashlib.sha256(inspect.getsource(fn).encode()).hexdigest() != HASHES[name]:
            raise RuntimeError('Diagnostic patch source mismatch; review required')

    @functools.wraps(original_process)
    def process(self, resp, raw=False):
        try:
            return original_process(self, resp, raw)
        except ResponseCodeException as exc:
            # Attach only the sanitized view to the same exception; no logging
            # hooks or additional HTTP request and no inference from Cookies.
            exc._dot_safe_response = summarize_response(resp.code, exc.raw)
            raise
        except NetworkException as exc:
            parsed_json = True
            try:
                payload = json.loads(resp.utf8_text())
            except (ValueError, UnicodeError):
                parsed_json = False
                payload = None
            exc._dot_safe_response = summarize_response(resp.code, payload)
            if not parsed_json:
                exc._dot_safe_response['body_format'] = 'non_json_or_unavailable'
                exc._dot_safe_response['body_structure'] = {'state':'redacted', 'type':'non_json_body'}
            raise

    @functools.wraps(original_error)
    def from_exception(exc):
        if isinstance(exc, NetworkException):
            status = exc.status if type(exc.status) is int else {'state':'unknown'}
            diagnostic = getattr(exc, '_dot_safe_response', {'http_status':status, 'body_format':'unavailable'})
            return errors.BiliError('risk_control' if status == 412 else 'network_error',
                'HTTP 请求失败；响应正文仅以脱敏 diagnostic 展示。',
                hint='不要自动重试互动写请求；先核对结果和诊断。', detail={'diagnostic':diagnostic})
        if not isinstance(exc, ResponseCodeException): return original_error(exc)
        diagnostic = getattr(exc, '_dot_safe_response', None)
        if diagnostic is None:
            diagnostic = summarize_response({'state':'unknown'}, exc.raw)
        message = exc.msg if safe_text(exc.msg) else '[服务端消息已脱敏；见 diagnostic 结构]'
        business_code = exc.code if type(exc.code) is int else shape(exc.code)
        code = errors.BILI_CODE_MAP.get(exc.code, 'api_error') if type(exc.code) is int else 'api_error'
        hint = None
        if type(exc.code) is int and exc.code in (-401, -403):
            hint = '服务端拒绝请求；原因尚未确认。不要自动重试互动写请求，先检查脱敏 diagnostic。'
        return errors.BiliError(code, f'B 站返回错误 {exc.code if type(exc.code) is int else "[异常业务码]"}：{message}', hint=hint,
                               detail={'bili_code':business_code, 'diagnostic':diagnostic})
    from_exception._dot_safe_diagnostics = True
    Api._process_response = process
    errors.from_exception = from_exception
