"""Synthetic responses only; no real credential reads or network."""
import sys, json, socket, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import response_diagnostics_patch
response_diagnostics_patch.apply()
from bili_safe_diagnostics import shape, summarize_response
from bilibili_api.utils.network import Api
from bilibili_api.exceptions import ResponseCodeException, NetworkException
from bili_cli.errors import from_exception

class Response:
    code=200
    headers={}
    def __init__(self,payload): self.payload=payload
    def utf8_text(self): return json.dumps(self.payload)

class Tests(unittest.TestCase):
    def test_sanitized_exception_keeps_http_and_decisions(self):
        payload={'code':-401,'message':'非法访问','data':{'ga_data':{'decisions':['reject'], 'risk_level':2, 'decision_ctx':{'scene':'video_coin','origin_scene':'web','decision_type':'deny','buvid':'DEVICE_SECRET','ip':'1.2.3.4','mid':'123456','v_voucher':'TICKET_SECRET','referer':'https://x/?token=SECRET'}}}}
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network prohibited')):
            with self.assertRaises(ResponseCodeException) as raised:
                Api(url='https://api.bilibili.com/x/web-interface/coin/add',method='POST')._process_response(Response(payload))
        output=from_exception(raised.exception).to_dict()
        diag=output['detail']['diagnostic']
        self.assertEqual(diag['http_status'],200)
        self.assertEqual(diag['business_code'],-401)
        ga=diag['ga_data']['fields']
        self.assertEqual(ga['decisions']['items'][0]['value'],'reject')
        self.assertEqual(ga['decision_ctx']['fields']['scene']['value'],'video_coin')
        self.assertEqual(ga['risk_level']['value'],2)
        text=json.dumps(output)
        for value in ('DEVICE_SECRET','TICKET_SECRET','1.2.3.4','123456','https://x'): self.assertNotIn(value,text)
        self.assertIn('原因尚未确认',output['hint'])
    def test_key_collision_and_truncation(self):
        result=shape({'bad key':1,'also bad':2,'安全键':3,'scene':'web'})
        self.assertEqual(len(result['fields']),4)
        self.assertEqual(result['fields']['scene']['value'],'web')
        self.assertEqual(shape(list(range(20)))['omitted_items'],4)
        self.assertEqual(shape({f'k{i}':i for i in range(90)})['omitted_fields'],10)
    def test_secret_in_allowed_fields_redacted(self):
        for value in ['token=abc','Bearer abc','1.2.3.4','https://evil/?x=1','12345678','Cookie abc','abcdefghijklmnopqrstuvwxyz012345','IP 2001:db8::1','2001:db8::1','uid 1234','User ID 4321']:
            self.assertEqual(shape({'scene':value})['fields']['scene']['state'],'redacted',value)
    def test_sensitive_values_in_keys(self):
        text=json.dumps(shape({'token_abcdEFGH1234':'hidden','uid_1234567':'x','token':None,'mid':None}))
        self.assertNotIn('abcdEFGH1234',text)
        self.assertNotIn('1234567',text)
        self.assertIn('redacted_key_0',text)
        self.assertIn('redacted_key_1',text)

    def test_sensitive_ancestry_blocks_safe_named_children(self):
        value = shape({'token': {'scene': 'otherwise_safe', 'risk_level': 1}})
        self.assertNotIn('otherwise_safe', json.dumps(value))
        self.assertEqual(value['fields']['token']['fields']['[redacted_key_1]']['state'], 'redacted')

    def test_missing_null_empty_and_unknown_http(self):
        for payload,state in [({},'missing'),({'data':None},'null'),({'data':{}},'empty_object')]:
            self.assertEqual(summarize_response(200,payload)['data']['state'],state)
        error=from_exception(ResponseCodeException(-403,'账号异常',{'code':-403,'message':'账号异常'}))
        self.assertEqual(error.detail['diagnostic']['http_status'],{'state':'unknown'})
        self.assertNotIn('降低',error.hint)
    def test_http_failures_do_not_leak_response(self):
        for status in (403,412,500):
            for body in ({'message':'token=TEST_SECRET','data':{'ga_data':{}}}, 'token=TEST_SECRET'):
                resp=Response(body);resp.code=status
                if isinstance(body,str): resp.utf8_text=lambda: body
                with self.assertRaises(NetworkException) as raised:
                    Api(url='https://api.bilibili.com/x/test',method='GET')._process_response(resp)
                out=from_exception(raised.exception).to_dict()
                self.assertNotIn('TEST_SECRET',json.dumps(out))
                self.assertEqual(out['detail']['diagnostic']['http_status'],status)
                self.assertIn('不要自动重试',out['hint'])

    def test_http_json_null_distinct_from_unparseable(self):
        resp=Response(None);resp.code=500
        with self.assertRaises(NetworkException) as raised:
            Api(url='https://api.bilibili.com/x/test',method='GET')._process_response(resp)
        diag=from_exception(raised.exception).detail['diagnostic']
        self.assertEqual(diag['body_format'],'non_object_json')
        self.assertEqual(diag['body_structure']['state'],'null')

    def test_malformed_business_codes(self):
        for code in ('token=TEST_SECRET',{'secret':'TEST_SECRET'}, ['TEST_SECRET'], True):
            out=from_exception(ResponseCodeException(code,'deny',{'code':code})).to_dict()
            self.assertNotIn('TEST_SECRET',json.dumps(out))
            self.assertEqual(out['code'],'api_error')

    def test_idempotent(self): response_diagnostics_patch.apply()

if __name__=='__main__': unittest.main(verbosity=2)
