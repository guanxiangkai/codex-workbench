"""原生首屏缓存与宿主传输边界；不开放原生 HTTP 直连。"""
import http.client
import json
import re
import threading
import unittest
import test_readonly_service as readonly
from codex_workbench.api import PreviewServer

class NativePageTests(unittest.TestCase):
    setUp=readonly.ReadonlyWorkbenchTest.setUp
    tearDown=readonly.ReadonlyWorkbenchTest.tearDown
    def prepare(self):
        class Versions:
            epoch='a'
            def context(self):return self.epoch
            def signature(self,view):return view
        self.service.source_versions=Versions()
        self.release.manifest.return_value={'html':"<html><script>const INITIAL_PAGE='board';const WORKBENCH_BOOTSTRAP=null;</script></html>",'resource_uri':'ui://test'}
    def bootstrap(self,page):return json.loads(re.search(r'const WORKBENCH_BOOTSTRAP=(.*?);</script>',page['html']).group(1))
    def test_native_page_contains_initial_cache_without_waiting_for_bridge(self):
        self.prepare();page=self.service.page(native=True);data=self.bootstrap(page)
        self.assertTrue(data['views']);self.assertNotIn('transport',data);self.assertEqual([],page['csp']['connectDomains'])
        self.assertEqual('board',data['views'][0]['data']['view']);self.assertEqual(1,self.native.calls.count('snapshot'))
        self.service.page(native=True);self.assertEqual(1,self.native.calls.count('snapshot'))
        self.assertNotIn('capability',self.release.manifest.return_value['html'])
    def test_native_page_has_no_preview_dependency(self):
        self.prepare();page=self.service.page(native=True);data=self.bootstrap(page)
        self.assertTrue(data['views']);self.assertNotIn('error',data);self.assertNotIn('transport',data)
        self.assertEqual([],page['csp']['connectDomains'])

    def test_bootstrap_json_is_html_safe(self):
        self.prepare();self.native.skills=lambda:[{'id':'a','name':'</script><img src=x onerror=alert(1)>'}]
        page=self.service.page('agents');self.assertNotIn('<img',page['html']);self.assertIn('\\u003c',page['html'])
    def test_account_switch_drops_old_bootstrap(self):
        self.prepare();first=self.bootstrap(self.service.page(native=True));self.service.source_versions.epoch='b'
        second=self.bootstrap(self.service.page(native=True))
        self.assertEqual('a',first['context']);self.assertEqual('b',second['context']);self.assertEqual(2,self.native.calls.count('snapshot'))
    def test_preview_rejects_cross_origin_access(self):
        self.prepare();server=PreviewServer(('127.0.0.1',0),self.service);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for method in ['POST','OPTIONS']:
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                conn.request(method,'/view-rpc',headers={'Origin':'null','Access-Control-Request-Method':'POST'})
                response=conn.getresponse();response.read();self.assertEqual(403,response.status);self.assertIsNone(response.getheader('Access-Control-Allow-Origin'));conn.close()
        finally:server.shutdown();server.server_close();thread.join(2)
