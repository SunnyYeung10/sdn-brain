"""Local experiment controller UI and API. Run: python -m sdn.server."""
import copy
import hmac
import ipaddress
import json
import os
from pathlib import Path
import threading
import time
from sanic import Sanic
from sanic.response import json as response_json
from sanic.exceptions import SanicException
from .load import LoadMonitor
from requests.utils import urlparse

from . import demo
from .onos import ONOS
from .routing import plan, validate_demand

ROOT = Path(__file__).resolve().parent.parent


def validate_batch(items):
    if not isinstance(items, list) or len(items) > 64:
        raise ValueError('需求应为数组，最多 64 项')
    result = [validate_demand(d) for d in items]
    ids, selectors = set(), set()
    for d in result:
        if d['id'] in ids:
            raise ValueError('需求 ID 重复')
        ids.add(d['id'])
        dst = d['group'] if d['kind'] == 'multicast' else d['receivers'][0]
        pairs = [(d['source'], dst)]
        if d['bidirectional']:
            pairs.append((dst, d['source']))
        for pair in pairs:
            if pair in selectors:
                raise ValueError('通信对选择器重叠；请合并相同通信对的需求')
            selectors.add(pair)
    return result


class Brain:
    def __init__(self, mode=None, state_path=None):
        self.lock = threading.RLock()
        self.mode = mode or os.getenv('SDN_MODE', 'demo')
        if self.mode not in ('demo', 'onos'):
            raise ValueError('SDN_MODE 应为 demo 或 onos')
        self.path = Path(state_path or os.getenv('SDN_STATE', str(ROOT / 'data' / (self.mode + '-demands.json'))))
        self.demands = validate_batch(json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists()
                                      else demo.demands() if self.mode == 'demo' else [])
        metric_path = os.getenv('SDN_METRICS', str(ROOT / 'config' / 'metrics.json'))
        metrics = json.loads(Path(metric_path).read_text(encoding='utf-8'))
        self.onos = ONOS(os.getenv('ONOS_URL', 'http://127.0.0.1:8181'),
                         os.getenv('ONOS_USER', 'onos'), os.getenv('ONOS_PASSWORD', 'rocks'), metrics)
        self.topo = demo.topology() if self.mode == 'demo' else dict(devices=[], links=[], hosts=[])
        self.revision, self.updated = 1, 0
        self.connected = self.mode == 'demo'
        self.error, self.telemetry_error = '', ''
        self.result = plan(self.topo, self.demands)
        self.plan_revision = 1
        self.managed = False
        self.install = dict(state='idle', count=0)
        self.events = []
        self.telemetry = []
        self.samples = {}
        self.stop = threading.Event()
        self.load_monitor = LoadMonitor()
        self.last_load_change = 0

    def event(self, text):
        self.events.insert(0, dict(time=time.time(), text=text))
        del self.events[30:]

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(mode=self.mode, connected=self.connected, error=self.error,
                topology=self.topo, demands=self.demands, plan=self.result,
                revision=self.revision, plan_revision=self.plan_revision,
                updated=self.updated, managed=self.managed, install=self.install,
                telemetry=self.telemetry, telemetry_error=self.telemetry_error, events=self.events))

    def update_demands(self, items, revision):
        items = validate_batch(items)
        with self.lock:
            if revision != self.revision:
                raise ValueError('数据已更新，请刷新后重试')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.path)
            self.demands = items
            self.revision += 1
            self.event('通信需求已保存，等待重算')

    def observe(self, flows, topology):
        now, rates, samples = time.monotonic(), [], {}
        hosts = {h['ip']: h for h in topology['hosts']}
        for f in flows:
            c = {x['type']: x for x in f.get('selector', {}).get('criteria', [])}
            if not all(k in c for k in ('IPV4_SRC', 'IPV4_DST', 'IN_PORT')):
                continue
            try:
                src_net = ipaddress.ip_network(c['IPV4_SRC']['ip'])
                dst_net = ipaddress.ip_network(c['IPV4_DST']['ip'])
                if src_net.prefixlen != 32 or dst_net.prefixlen != 32:
                    continue
                src, dst = str(src_net.network_address), str(dst_net.network_address)
                h = hosts.get(src)
                if not h or h['device'] != f['deviceId'] or str(h['port']) != str(c['IN_PORT']['port']):
                    continue  # Sample only source ingress, no hop double-counting.
                key = (f['deviceId'], f['id'])
                count = int(f.get('bytes', 0))
                samples[key] = (now, count)
                previous = self.samples.get(key)
                rate = (max(0, count - previous[1]) * 8 / (now - previous[0]) / 1e6
                        if previous and now > previous[0] else None)
                rates.append(dict(source=src, destination=dst, mbps=rate, bytes=count))
            except (ValueError, KeyError, TypeError):
                continue
        self.samples, self.telemetry = samples, rates

    def run(self):
        next_poll, applied_revision, last_apply = 0, -1, 0
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + 1
                try:
                    topo = self.onos.topology() if self.mode == 'onos' else None
                    flows = self.onos.flows() if self.mode == 'onos' else []
                    if topo is not None:
                        with self.lock:
                            reservations = {}
                            for d in self.demands:
                                accepted = next((p for p in self.result['demands'] if p['id'] == d['id'] and p['status'] == 'accepted'), None)
                                if accepted:
                                    for eid in set(accepted['edges']):
                                        reservations[eid] = reservations.get(eid, 0) + d['mbps']
                        topo = self.load_monitor.update(topo, self.onos.port_statistics(), flows, time.monotonic(), reservations)
                    with self.lock:
                        if topo is not None:
                            def structural(t):
                                return (t['devices'], t['hosts'], [(e['id'],e.get('load_valid'),e['up']) for e in t['links']])
                            changed = structural(topo) != structural(self.topo)
                            # Keep old routing metrics during the five-second hold-down.
                            if not changed and now-self.last_load_change < 5:
                                old = {e['id']: e for e in self.topo['links']}
                                for e in topo['links']:
                                    for field in ('cost','capacity'):
                                        e[field] = old[e['id']][field]
                            metrics_changed = [(e['id'],round(e['cost'],3),round(e['capacity'],1)) for e in topo['links']] != [(e['id'],round(e['cost'],3),round(e['capacity'],1)) for e in self.topo['links']]
                            if changed or metrics_changed:
                                self.revision += 1
                                self.last_load_change = now
                                self.event('拓扑或链路负载变化，重新计算路径')
                            self.topo = topo
                            self.observe(flows, topo)
                        self.connected, self.error, self.updated = True, '', time.time()
                except Exception as exc:
                    with self.lock:
                        self.connected, self.error = False, str(exc)
                        self.install = dict(state='offline', count=0)
            with self.lock:
                revision = self.revision
                topo, demands = copy.deepcopy(self.topo), copy.deepcopy(self.demands)
                dirty = self.plan_revision != revision
            if dirty:
                try:
                    result = plan(topo, demands)
                    with self.lock:
                        if self.revision == revision:
                            self.result, self.plan_revision = result, revision
                            self.event('路径计算完成')
                except Exception as exc:
                    with self.lock:
                        self.error = str(exc)
                        self.plan_revision = -1
            with self.lock:
                apply_now = self.managed and self.connected and self.plan_revision == self.revision and (
                    applied_revision != self.revision or now - last_apply >= 10)
                rules = copy.deepcopy(self.result['rules'])
                revision = self.revision
                if apply_now:
                    self.install = dict(state='applying', count=len(rules))
            if apply_now:
                try:
                    installed = (self.onos.apply(rules) if self.mode == 'onos'
                                 else dict(state='simulated', count=len(rules), time=time.time()))
                    with self.lock:
                        self.install = installed
                        if applied_revision != revision:
                            self.event('流表已核验' if self.mode == 'onos' else '模拟下发完成（未连接真实交换机）')
                    applied_revision, last_apply = revision, time.monotonic()
                except Exception as exc:
                    with self.lock:
                        self.install = dict(state='failed', error=str(exc), count=0)
                        self.event('下发失败：' + str(exc))
                    # Retry on the next polling cycle, not in a tight loop.
                    self.stop.wait(5)
            self.stop.wait(.25)


def create_app(brain=None):
    app = Sanic('sdn_brain')
    app.config.REQUEST_MAX_SIZE = 65536
    brain = brain or Brain()
    app.ctx.brain = brain
    token = os.getenv('SDN_API_TOKEN', '')
    host, port = os.getenv('SDN_HOST','127.0.0.1'), int(os.getenv('SDN_PORT','8000'))
    if host not in ('127.0.0.1','localhost') and not token:
        raise ValueError('监听局域网地址必须设置 SDN_API_TOKEN')
    allowed = {f'localhost:{port}',f'127.0.0.1:{port}',f'{host}:{port}'}
    allowed.update(filter(None,os.getenv('SDN_ALLOWED_HOSTS','').split(',')))

    @app.middleware('request')
    async def protect(request):
        if not request.path.startswith('/api/'):
            return
        if token and not hmac.compare_digest(request.headers.get('X-API-Token',''),token):
            return response_json({'error':'访问令牌错误'},status=401)
        authority = request.headers.get('host','')
        if authority not in allowed or (request.headers.get('origin') and urlparse(request.headers['origin']).netloc != authority):
            return response_json({'error':'禁止跨站或未知 Host 请求'},status=403)

    @app.get('/api/state')
    async def state(request):
        return response_json(brain.snapshot())

    @app.post('/api/<action:path>')
    async def mutate(request,action):
        if request.content_type != 'application/json':
            raise ValueError('请求必须为 application/json')
        data = request.json
        with brain.lock:
            if action == 'demands':
                brain.update_demands(data['demands'],data['revision'])
            elif action == 'apply':
                if not brain.connected or data['revision'] != brain.revision or brain.plan_revision != brain.revision:
                    raise ValueError('控制器离线或计算版本已变化，请刷新')
                brain.managed = True
                brain.event('已启用负载感知闭环下发')
            elif action == 'pause':
                brain.managed = False
            elif action == 'demo/link' and brain.mode == 'demo':
                edge=next(e for e in brain.topo['links'] if e['id']==data['id'])
                for e in brain.topo['links']:
                    if {e['src'],e['dst']} == {edge['src'],edge['dst']}:
                        e['up']=bool(data['up'])
                brain.revision += 1
            elif action == 'demo/load' and brain.mode == 'demo':
                edge=next(e for e in brain.topo['links'] if e['id']==data['id'])
                load=float(data['mbps'])
                import math
                cap=edge.get('physical_capacity',edge['capacity'])
                if not math.isfinite(load) or not 0 <= load <= cap:
                    raise ValueError('模拟背景负载必须在 0 与链路容量之间')
                base=edge.get('base_cost',edge['cost'])
                edge.update(base_cost=base,physical_capacity=cap,background_mbps=load,
                            measured_mbps=load,utilization=load/cap,load_valid=True,
                            capacity=cap-load,cost=base*(1+4*(round(load/cap*10)/10)**2))
                brain.revision += 1
                brain.event('模拟有向链路背景负载变化，触发重算')
            else:
                return response_json({'error':'接口不存在'},status=404)
        return response_json({'ok':True})

    @app.exception(ValueError, KeyError, TypeError, StopIteration)
    async def invalid(request,exc):
        return response_json({'error':str(exc)},status=400)

    @app.exception(SanicException)
    async def http_error(request,exc):
        return response_json({'error':str(exc)},status=exc.status_code)

    @app.middleware('response')
    async def headers(request,response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'

    app.static('/',str(ROOT/'web/index.html'),name='index')
    app.static('/app.js',str(ROOT/'web/app.js'),name='js')
    app.static('/style.css',str(ROOT/'web/style.css'),name='css')

    @app.before_server_start
    async def start(app):
        app.ctx.worker=threading.Thread(target=brain.run,daemon=True)
        app.ctx.worker.start()

    @app.after_server_stop
    async def stop(app):
        brain.stop.set()

    return app


def main():
    create_app().run(host=os.getenv('SDN_HOST','127.0.0.1'),port=int(os.getenv('SDN_PORT','8000')),
                     single_process=True,access_log=False)


if __name__ == '__main__':
    main()
