import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sdn.demo import topology
from sdn.load import LoadMonitor
from sdn.onos import ONOS, APP_ID
from sdn.routing import plan, validate_demand
from sdn.server import Brain, create_app


def stats(t, stamp, rates):
    devices = {}
    for e in t['links']:
        devices.setdefault(e['src'], []).append(dict(port=e['src_port'], durationSec=stamp,
            bytesSent=int(rates.get(e['id'], 0)*1e6/8*stamp)))
    return {'statistics':[{'device':k,'ports':v} for k,v in devices.items()]}


class LoadTests(unittest.TestCase):
    def test_load_reroutes_without_topology_change(self):
        t=topology(); m=LoadMonitor(alpha=1)
        d=validate_demand(dict(id='load',source='10.0.0.1',receivers=['10.0.0.2'],mbps=5))
        old=plan(t,[d])['demands'][0]['edges']
        rates={old[0]:90}
        m.update(copy.deepcopy(t),stats(t,10,rates),[],10)
        new=m.update(copy.deepcopy(t),stats(t,11,rates),[],11)
        result=plan(new,[d])['demands'][0]
        self.assertEqual(result['status'],'accepted')
        self.assertNotIn(old[0],result['edges'])
        self.assertAlmostEqual(new['links'][0]['measured_mbps'],90)

    def test_warmup_stale_and_reset(self):
        t=topology();m=LoadMonitor()
        warm=m.update(copy.deepcopy(t),stats(t,10,{}),[],10)
        self.assertTrue(all(not e['up'] for e in warm['links']))
        valid=m.update(copy.deepcopy(t),stats(t,11,{}),[],11)
        self.assertTrue(all(e['up'] for e in valid['links']))
        stale=m.update(copy.deepcopy(t),stats(t,11,{}),[],30)
        self.assertTrue(all(not e['up'] for e in stale['links']))
        reset=m.update(copy.deepcopy(t),stats(t,1,{}),[],31)
        self.assertTrue(all(not e['up'] for e in reset['links']))

    def test_managed_traffic_not_reserved_twice(self):
        t=topology();e=t['links'][0];m=LoadMonitor(alpha=1)
        def flow(count):
            return [dict(appId=APP_ID,state='ADDED',deviceId=e['src'],id='1',bytes=count,
                treatment={'instructions':[{'type':'OUTPUT','port':e['src_port']}]} )]
        m.update(copy.deepcopy(t),stats(t,10,{e['id']:20}),flow(0),10)
        new=m.update(copy.deepcopy(t),stats(t,11,{e['id']:20}),flow(2500000),11,{e['id']:20})
        self.assertEqual(new['links'][0]['background_mbps'],0)
        self.assertEqual(new['links'][0]['capacity'],100)

    def test_requests_adapter(self):
        client=ONOS('http://localhost:8181','u','p')
        with patch.object(client.session,'request') as request:
            request.return_value.content=b'{}'
            request.return_value.json.return_value={}
            self.assertEqual(client.request('GET','devices'),{})
            request.return_value.raise_for_status.assert_called_once()
            self.assertEqual(request.call_args.kwargs['timeout'],(3,8))


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_sanic_api_static_and_validation(self):
        scratch=Path(__file__).resolve().parent.parent/'data'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            brain=Brain('demo',Path(tmp)/'d.json')
            app=create_app(brain)
            _,r=await app.asgi_client.get('/api/state',headers={'host':'localhost:8000'})
            self.assertEqual(r.status,200)
            _,r=await app.asgi_client.get('/')
            self.assertEqual(r.status,200)
            _,r=await app.asgi_client.post('/api/demands',json={'revision':1,'demands':[]},headers={'host':'localhost:8000'})
            self.assertEqual(r.status,200)
            _,r=await app.asgi_client.post('/api/apply',json={'revision':1},headers={'host':'localhost:8000'})
            self.assertEqual(r.status,400)
            _,r=await app.asgi_client.get('/api/state',headers={'host':'evil.example'})
            self.assertEqual(r.status,403)
