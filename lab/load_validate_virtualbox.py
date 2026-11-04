"""Inject isolated background traffic and verify a real load-driven reroute."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid
import requests
from sdn.onos import APP_ID, fingerprint
from sdn.routing import shortest, compile_tree


def run_test(args):
    status=Path(args.status)
    client=requests.Session()
    client.headers['X-API-Token']=(status/'api-token').read_text().strip()
    onos=requests.Session();onos.auth=('onos','rocks')
    app='org.example.sdnbrain.loadtest.'+uuid.uuid4().hex[:8]
    def state():
        r=client.get('http://127.0.0.1:8000/api/state',timeout=8);r.raise_for_status();return r.json()
    def request(method,path,body=None):
        r=onos.request(method,'http://127.0.0.1:8181/onos/v1/'+path,json=body,timeout=(3,10));r.raise_for_status();return r.json() if r.content else {}
    before=state();topo=before['topology']
    hosts={h['ip']:h for h in topo['hosts']}
    links={e['id']:e for e in topo['links']}
    old=next(d for d in before['plan']['demands'] if d['id']=='vm-unicast')
    target=next(links[eid] for eid in old['edges'] if links[eid]['src']==hosts['10.0.0.2']['device'])
    source,destination=hosts['10.0.0.3'],hosts['10.0.0.4']
    pinned=(shortest(topo['links'],source['device'],target['src'])+[target]+
            shortest(topo['links'],target['dst'],destination['device']))
    nodes=[source['device']]+[e['dst'] for e in pinned]
    if len(nodes)!=len(set(nodes)):
        raise RuntimeError('Background path would contain a cycle')
    reverse=[]
    for e in reversed(pinned):
        reverse.append(next(r for r in topo['links'] if (r['src'],r['src_port'],r['dst'],r['dst_port'])==(e['dst'],e['dst_port'],e['src'],e['src_port'])))
    rules=compile_tree(pinned,source,[destination],destination['ip'])+compile_tree(reverse,destination,[source],source['ip'])
    for r in rules:r['priority']=42000
    metadata=json.loads(Path('/run/codex-sdn-lab-20261102.json').read_text())
    def command(h,*args):return ['mnexec','-a',str(int(metadata[h]['pid'])),*args]
    server=sender=None
    try:
        request('POST','flows?appId='+app,{'flows':rules})
        wanted={fingerprint(r) for r in rules}
        deadline=time.monotonic()+30
        while True:
            flows=request('GET','flows')['flows']
            if wanted <= {fingerprint(f) for f in flows if f['appId']==app and f['state']=='ADDED'}:break
            if time.monotonic()>deadline:raise RuntimeError('Background rules did not install')
            time.sleep(1)
        server=subprocess.Popen(command('h4','iperf3','-s','-1','-p','5209'),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        time.sleep(1)
        sender=subprocess.Popen(command('h3','iperf3','-c','10.0.0.4','-p','5209','-u','-b','1M','-t','70','-J'),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        started=time.monotonic();peak=0
        deadline=started+65
        while time.monotonic()<deadline:
            time.sleep(2)
            current=state()
            e=next(e for e in current['topology']['links'] if e['id']==target['id'])
            peak=max(peak,e.get('measured_mbps') or 0)
            route=next(d for d in current['plan']['demands'] if d['id']=='vm-unicast')
            if route['status']=='accepted' and target['id'] not in route['edges'] and peak>.4 and current['install']['state']=='verified':
                flows=request('GET','flows')['flows']
                expected={fingerprint(r) for r in current['plan']['rules']}
                if expected <= {fingerprint(f) for f in flows if f['appId']==APP_ID and f['state']=='ADDED'}:
                    (status/'load-reroute.json').write_text(json.dumps(dict(
                        passed=True,offered_mbps=1,configured_test_budget_mbps=1,physical_link_mbps=100,measured_peak_mbps=peak,changed_after_seconds=time.monotonic()-started,
                        loaded_edge=target['id'],before=old['edges'],after=route['edges'],
                        topology_changed=False,flow_install='ONOS ADDED',
                        note='Actual measured background traffic on a separately owned pinned path. The controller temporarily uses a 1 Mbps test budget, with original metrics and demands restored afterwards.'),indent=2))
                    print('Actual measured load caused a verified reroute. Peak Mbps:',round(peak,2))
                    return
            if sender.poll() is not None:
                output=sender.communicate()[0]
                (status/'load-client.txt').write_text(output)
                raise RuntimeError('Background traffic sender stopped before a verified reroute')
        raise RuntimeError('No verified load-driven reroute within 65 seconds; measured peak Mbps='+str(peak))
    finally:
        for p,name in [(sender,'load-client.txt'),(server,'load-server.txt')]:
            if p:
                if p.poll() is None:p.terminate()
                try:output=p.communicate(timeout=5)[0]
                except subprocess.TimeoutExpired:
                    p.kill();output=p.communicate()[0]
                (status/name).write_text(output or '')
        request('DELETE','flows/application/'+app)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--status',required=True)
    args=parser.parse_args()
    status=Path(args.status)
    client=requests.Session()
    client.headers['X-API-Token']=(status/'api-token').read_text().strip()
    def snapshot():
        r=client.get('http://127.0.0.1:8000/api/state',timeout=8);r.raise_for_status();return r.json()
    def restart_and_enable():
        subprocess.run(['systemctl','restart','codex-sdn-brain-20261102'],check=True,timeout=30)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            try:
                d=snapshot()
                if d['connected'] and len(d['topology']['hosts'])>=6 and all(e.get('load_valid') for e in d['topology']['links']) and d['revision']==d['plan_revision']:
                    r=client.post('http://127.0.0.1:8000/api/apply',json={'revision':d['revision']},timeout=8)
                    if r.ok:break
            except requests.RequestException:pass
            time.sleep(1)
        else:raise RuntimeError('Service did not become ready after metrics update')
        while time.monotonic()<deadline:
            d=snapshot()
            if d['install']['state']=='verified' and all(p['status']=='accepted' for p in d['plan']['demands']):return
            time.sleep(1)
        raise RuntimeError('Updated demand rules did not converge')
    before=snapshot()
    hosts={h['ip']:h for h in before['topology']['hosts']}
    edges={e['id']:e for e in before['topology']['links']}
    demand=next(d for d in before['plan']['demands'] if d['id']=='vm-unicast')
    target=next(eid for eid in demand['edges'] if edges[eid]['src']==hosts['10.0.0.2']['device'])
    metrics=Path('config/metrics.json'); demands=Path('data/onos-demands.json')
    saved_metrics=metrics.read_text();saved_demands=demands.read_text()
    try:
        config=json.loads(saved_metrics)
        config[target]={'cost':edges[target].get('base_cost',1),'capacity':1}
        metrics.write_text(json.dumps(config))
        lower=json.loads(saved_demands)
        for item in lower:item['mbps']=.1
        demands.write_text(json.dumps(lower))
        restart_and_enable()
        run_test(args)
    finally:
        metrics.write_text(saved_metrics)
        demands.write_text(saved_demands)
        restart_and_enable()
        report=status/'load-reroute.json'
        if report.exists():
            data=json.loads(report.read_text());data['original_configuration_restored']=True
            report.write_text(json.dumps(data,indent=2))


if __name__=='__main__':main()
