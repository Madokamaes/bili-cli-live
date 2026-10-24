"""Value-minimizing diagnostic helpers; no logging of request secrets or raw bodies."""
import json
import re
from urllib.parse import urlsplit
MISSING = object()
COOKIE_KEYS = ('SESSDATA', 'bili_jct', 'DedeUserID', 'buvid3', 'buvid4')
SAFE_MESSAGE = re.compile(r'^[\w\s\u3400-\u9fff,，。.!！?？:：()（）\-]{0,160}$')

SAFE_VALUE_KEYS = {'decisions', 'decision_type', 'origin_scene', 'scene', 'message', 'msg', 'error_description'}
SECRET_WORDS = re.compile(r'(?i)(sessdata|bili_jct|cookie|token|voucher|ticket|password|secret|authorization|bearer|buvid|csrf)')

IDENTITY_LABELS = re.compile(r'(?i)(\b(?:uid|mid|ip|ipv4|ipv6|account|device|user[ _-]*id)\b|账号|帐号|用户ID|设备标识|地址)')
SENSITIVE_KEY_NAMES = {'token', 'access_token', 'refresh_token', 'v_voucher', 'voucher', 'ticket', 'cookie', 'cookies', 'sessdata', 'bili_jct', 'csrf', 'csrf_token', 'buvid', 'buvid3', 'buvid4', 'mid', 'uid', 'ip', 'ua', 'referer', 'account', 'user_id', 'device_id', 'grisk_id', 'hrid', 'authorization', 'password', 'secret'}

SAFE_STRUCTURAL_KEYS = SAFE_VALUE_KEYS | SENSITIVE_KEY_NAMES | {'code', 'data', 'ga_data', 'decision_ctx', 'risk_level', 'ttl', 'status', 'error', 'reason', 'error_code', 'type', 'result', 'OK', 'description', 'success', 'nested', 'url'}

def safe_key_name(key):
    # Unknown JSON keys may themselves be token/ID values. Preserve only known
    # schema names; numbered redactions keep cardinality without leaking them.
    return isinstance(key, str) and key in SAFE_STRUCTURAL_KEYS

# Fail closed: fields called scene/message can still contain credentials or IDs.
# Only reviewed literal public labels/messages are released, not arbitrary prose.
SAFE_LITERAL_VALUES = frozenset({
    'allow', 'deny', 'reject', 'block', 'pass', 'verify', 'captcha', 'challenge',
    'web', 'video', 'video_coin', 'video_like', 'coin', 'like', 'comment',
    '非法访问', '账号异常,操作失败', '账号异常，操作失败', '账号异常',
    '请求错误', '请求被拦截', '成功', '0', 'mock',
})

def safe_text(value):
    return isinstance(value, str) and value in SAFE_LITERAL_VALUES

def shape(value=MISSING, depth=0, field=None, blocked=False):
    blocked = blocked or (isinstance(field, str) and (bool(SECRET_WORDS.search(field)) or field.lower() in {"mid", "uid", "ip", "ua", "referer", "account", "user_id", "device_id", "grisk_id", "hrid"}))
    if value is MISSING: return {'state': 'missing'}
    if value is None: return {'state': 'null'}
    if isinstance(value, dict):
        if not value: return {'state': 'empty_object'}
        if depth >= 8: return {'state': 'redacted', 'type': 'object', 'reason': 'depth_limit'}
        children = {}
        for index, (k, v) in enumerate(list(value.items())[:80]):
            safe_key = k if not blocked and safe_key_name(k) else f'[redacted_key_{index}]'
            children[safe_key] = shape(v, depth + 1, k, blocked)
        result = {'state': 'object', 'fields': children}
        if len(value) > 80: result['omitted_fields'] = len(value) - 80
        return result
    if isinstance(value, list):
        if not value: return {'state': 'empty_array'}
        if depth >= 8: return {'state':'redacted', 'type':'array', 'reason':'depth_limit'}
        result = {'state': 'array', 'items': [shape(x, depth + 1, field, blocked) for x in value[:16]]}
        if len(value) > 16: result['omitted_items'] = len(value) - 16
        return result
    kind = 'boolean' if isinstance(value,bool) else 'number' if isinstance(value,(int,float)) else 'string' if isinstance(value,str) else 'other'
    if not blocked and field in SAFE_VALUE_KEYS and safe_text(value):
        return {'state': 'value', 'type': kind, 'value': value}
    if not blocked and field == 'risk_level' and type(value) is int and 0 <= value <= 100:
        return {'state': 'value', 'type': kind, 'value': value}
    return {'state':'redacted', 'type':kind}

def summarize_response(http_status, payload):
    result = {'http_status': http_status, 'body_format': 'json' if isinstance(payload,dict) else 'non_object_json', 'body_structure': shape(payload)}
    if not isinstance(payload,dict): return result
    code = payload.get('code', MISSING)
    result['business_code'] = code if isinstance(code,int) and not isinstance(code,bool) else shape(code)
    for key in ('message','msg'):
        value = payload.get(key,MISSING)
        result[key] = {'state':'value','value':value} if safe_text(value) else shape(value)
    result['data'] = shape(payload.get('data',MISSING))
    if isinstance(payload.get('data'),dict): result['ga_data'] = shape(payload['data'].get('ga_data',MISSING))
    else: result['ga_data'] = {'state':'unavailable_parent'}
    return result

def summarize_request(method, url, data=None, cookies=None, credential_csrf=None):
    data = data if isinstance(data,dict) else {}
    cookies = cookies or {}
    return {'method':method.upper(), 'host':urlsplit(url).hostname, 'endpoint':urlsplit(url).path,
      'cookie_presence':{k:bool(cookies.get(k)) for k in COOKIE_KEYS},
      'csrf_presence':{k:bool(data.get(k)) for k in ('csrf','csrf_token')},
      'csrf_matches_loaded':{k:bool(credential_csrf) and data.get(k)==credential_csrf for k in ('csrf','csrf_token')},
      'csrf_matches_cookie':{k:bool(cookies.get('bili_jct')) and data.get(k)==cookies.get('bili_jct') for k in ('csrf','csrf_token')}}

class WriteBlocked(RuntimeError): pass

def assert_allowed(method, url, cookies=None, headers=None):
    p = urlsplit(url)
    if (method.upper() != 'GET' or p.scheme != 'https' or p.netloc != 'api.bilibili.com'
        or p.path != '/x/frontend/finger/spi' or p.query or cookies
        or any(k.lower() in ('cookie','authorization','proxy-authorization') for k in (headers or {}))):
        raise WriteBlocked('Diagnostic guard blocked request before transport')
