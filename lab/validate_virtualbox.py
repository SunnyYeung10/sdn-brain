"""Post-deployment real dataplane verification. Run as root in Linux."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', required=True)
    args = parser.parse_args()
    status = Path(args.status)
    client = requests.Session()
    client.headers['X-API-Token'] = (status/'api-token').read_text().strip()
    base = 'http://127.0.0.1:8000'

    def get():
        r = client.get(base+'/api/state', timeout=8)
        r.raise_for_status()
        return r.json()

    deadline = time.monotonic()+180
    while True:
        try:
            state = get()
            if len(state['topology']['hosts']) >= 6 and state['connected'] and all(e.get('load_valid') for e in state['topology']['links']) and state['plan_revision'] == state['revision']:
                break
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Topology, host discovery or valid load samples did not converge')
        time.sleep(2)
    demands = [dict(id='vm-unicast',kind='unicast',source='10.0.0.2',receivers=['10.0.0.6'],mbps=5,priority=5,bidirectional=True),
               dict(id='vm-multicast',kind='multicast',source='10.0.0.1',receivers=['10.0.0.4','10.0.0.5','10.0.0.6'],group='239.1.1.1',mbps=5,priority=10)]
    def post(path,body):
        r=client.post(base+path,json=body,timeout=10)
        r.raise_for_status()
    # Retry optimistic concurrency conflicts while load samples update.
    for _ in range(20):
        state=get()
        try:
            post('/api/demands',{'revision':state['revision'],'demands':demands})
            break
        except requests.HTTPError as exc:
            if exc.response.status_code != 400:
                raise
            time.sleep(1)
    else:
        raise RuntimeError('Could not register demands due to changing revisions')
    for _ in range(30):
        state=get()
        if state['plan_revision'] == state['revision']:
            try:
                post('/api/apply',{'revision':state['revision']})
                break
            except requests.HTTPError as exc:
                if exc.response.status_code != 400:
                    raise
        time.sleep(1)
    else:
        raise RuntimeError('Could not enable closed-loop control')
    deadline=time.monotonic()+180
    while True:
        state=get()
        if state['install']['state']=='verified' and len(state['plan']['demands'])==2 and all(d['status']=='accepted' for d in state['plan']['demands']):
            break
        if time.monotonic()>deadline:
            (status/'state-failed.json').write_text(json.dumps(state,ensure_ascii=False,indent=2))
            raise RuntimeError('ONOS did not verify both traffic demands')
        time.sleep(2)
    hosts=json.loads(Path('/run/codex-sdn-lab-20261102.json').read_text())
    def command(host,*cmd):
        pid=int(hosts[host]['pid'])
        if not Path(f'/proc/{pid}').exists():
            raise RuntimeError('Mininet host process has stopped')
        return ['mnexec','-a',str(pid),*cmd]
    ping=subprocess.run(command('h2','ping','-c','5','-W','2','10.0.0.6'),capture_output=True,text=True,timeout=20)
    (status/'unicast-ping.txt').write_text(ping.stdout+ping.stderr)
    if ping.returncode:
        raise RuntimeError('Unicast ping failed; inspect unicast-ping.txt')
    processes=[]
    files=[]
    traffic=str(Path('lab/traffic.py').resolve())
    try:
        for host in ('h4','h5','h6'):
            file=(status/f'{host}-multicast.txt').open('w')
            files.append(file)
            processes.append(subprocess.Popen(command(host,'python3',traffic,'receive','--interface',hosts[host]['ip'],'--seconds','15'),stdout=file,stderr=subprocess.STDOUT))
        time.sleep(2)
        send=subprocess.run(command('h1','python3',traffic,'send','--interface','10.0.0.1','--seconds','8','--mbps','1'),capture_output=True,text=True,timeout=30)
        (status/'multicast-send.txt').write_text(send.stdout+send.stderr)
        send.check_returncode()
        for process in processes:
            if process.wait(timeout=20):
                raise RuntimeError('Multicast receiver exited with an error')
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        for file in files:
            file.close()
    for host in ('h4','h5','h6'):
        value=(status/f'{host}-multicast.txt').read_text()
        if 'receive: 0 packets' in value or 'duplicate packets: 0' not in value:
            raise RuntimeError('Multicast data validation failed: '+host)
    state=get()
    (status/'state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2))
    (status/'result.json').write_text(json.dumps(dict(unicast_ping='passed',multicast_receivers=['h4','h5','h6'],
        onos_install=state['install'],dynamic_load='collector active; background-load reroute still requires a separate traffic experiment'),indent=2))
    print('Actual unicast ping and multicast reception passed.')


if __name__=='__main__':
    main()
