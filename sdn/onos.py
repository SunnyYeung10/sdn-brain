import json
import math
import time
import requests

APP_ID = 'org.example.sdnbrain'


def fingerprint(rule):
    criteria = []
    for item in rule['selector']['criteria']:
        item = dict(item)
        if 'ethType' in item:
            item['ethType'] = int(str(item['ethType']), 0)
        if 'port' in item:
            item['port'] = str(item['port'])
        criteria.append(item)
    instructions = []
    for item in rule['treatment'].get('instructions', []):
        item = dict(item)
        if 'port' in item:
            item['port'] = str(item['port'])
        instructions.append(item)
    return json.dumps(dict(deviceId=rule['deviceId'], priority=rule['priority'],
                           isPermanent=bool(rule.get('isPermanent', False)),
                           criteria=sorted(criteria, key=lambda x: json.dumps(x, sort_keys=True)),
                           instructions=sorted(instructions, key=lambda x: json.dumps(x, sort_keys=True))), sort_keys=True)


class ONOS:
    def __init__(self, url, user, password, metrics=None):
        self.url = url.rstrip('/') + '/onos/v1/'
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.metrics = metrics or {}

    def request(self, method, path, body=None):
        response = self.session.request(method, self.url + path, json=body, timeout=(3, 8))
        response.raise_for_status()
        return response.json() if response.content else {}

    def port_statistics(self):
        return self.request('GET', 'statistics/ports')

    def topology(self):
        devices = self.request('GET', 'devices')['devices']
        active = {d['id'] for d in devices if d.get('available') is True}
        links = []
        for e in self.request('GET', 'links')['links']:
            a, b = e['src'], e['dst']
            if e.get('state', 'ACTIVE') != 'ACTIVE' or a['device'] not in active or b['device'] not in active:
                continue
            eid = f"{a['device']}/{a['port']}>{b['device']}/{b['port']}"
            metric = self.metrics.get(eid, self.metrics.get('default', {}))
            cost, capacity = float(metric.get('cost', 1)), float(metric.get('capacity', 100))
            if not math.isfinite(cost) or not math.isfinite(capacity) or cost <= 0 or capacity <= 0:
                raise ValueError('链路 cost 和 capacity 必须为有限正数')
            links.append(dict(id=eid, src=a['device'], dst=b['device'],
                              src_port=str(a['port']), dst_port=str(b['port']),
                              cost=cost, capacity=capacity, up=True))
        hosts = []
        for h in self.request('GET', 'hosts')['hosts']:
            locations = [x for x in h.get('locations', []) if x['elementId'] in active]
            if len(locations) != 1 or str(h.get('vlan', 'None')) not in ('None', '-1'):
                continue  # Prototype supports one attachment and untagged IPv4.
            for ip in h.get('ipAddresses', []):
                if ':' not in ip:
                    hosts.append(dict(id=h['id'], ip=ip, device=locations[0]['elementId'],
                                      port=str(locations[0]['port']), label=ip))
        return dict(devices=[dict(id=d['id'], label=d.get('annotations', {}).get('datapathDescription', d['id'][-4:])) for d in devices if d['id'] in active],
                    links=sorted(links, key=lambda e: e['id']), hosts=sorted(hosts, key=lambda h: h['ip']))

    def flows(self):
        return self.request('GET', 'flows')['flows']

    def apply(self, rules):
        """Reconcile this app only. Verify ADDED before removing obsolete rules.

        OpenFlow REST operations are not an atomic network-wide transaction.
        Caller must report failure and retain previous desired state for retry.
        """
        current = [f for f in self.flows() if f.get('appId') == APP_ID]
        desired = {fingerprint(r) for r in rules}
        ready = {fingerprint(f) for f in current if f.get('state') == 'ADDED'}
        missing = [r for r in rules if fingerprint(r) not in ready]
        if missing:
            self.request('POST', 'flows?appId=' + APP_ID, {'flows': missing})
            deadline = time.monotonic() + 20
            while True:
                observed = [f for f in self.flows() if f.get('appId') == APP_ID]
                added = {fingerprint(f) for f in observed if f.get('state') == 'ADDED'}
                if desired <= added:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('ONOS 未确认流表 ADDED；已接受的部分规则可能仍存在，请检查并重试')
                time.sleep(.25)
        # Treatment updates may retain the same flow ID. Re-read before deleting
        # so an old snapshot cannot delete a newly replaced rule with that ID.
        for f in [f for f in self.flows() if f.get('appId') == APP_ID]:
            if fingerprint(f) not in desired:
                self.request('DELETE', 'flows/' + requests.utils.quote(f['deviceId'], safe='') + '/' + str(f['id']))
        # Removal is asynchronous; do not report success until gone.
        deadline = time.monotonic() + 20
        while True:
            observed = [f for f in self.flows() if f.get('appId') == APP_ID]
            if all(fingerprint(f) in desired and f.get('state') == 'ADDED' for f in observed) and desired <= {fingerprint(f) for f in observed}:
                return {'state': 'verified', 'count': len(rules), 'time': time.time()}
            if time.monotonic() >= deadline:
                raise RuntimeError('流表删除/安装尚未收敛，请检查 ONOS 和 OVS')
            time.sleep(.25)
