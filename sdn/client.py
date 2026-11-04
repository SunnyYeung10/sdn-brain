"""Business applications can declare demand before sending packets."""
import argparse
import json
import os
import requests


class Client:
    def __init__(self, url='http://127.0.0.1:8000', token=''):
        self.url, self.token = url.rstrip('/'), token

    def request(self, path, body=None):
        response = requests.request('POST' if body is not None else 'GET', self.url + path,
            json=body, headers={'X-API-Token': self.token}, timeout=(3,10))
        response.raise_for_status()
        return response.json()

    def register(self, demand):
        state = self.request('/api/state')
        demands = [d for d in state['demands'] if d['id'] != demand['id']] + [demand]
        return self.request('/api/demands', dict(revision=state['revision'], demands=demands))

    def remove(self, demand_id):
        state = self.request('/api/state')
        return self.request('/api/demands', dict(revision=state['revision'],
            demands=[d for d in state['demands'] if d['id'] != demand_id]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='应用主动上报通信需求；接收端须已被 ONOS 发现')
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--id', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--receivers', nargs='+', required=True)
    parser.add_argument('--mbps', type=float, default=1)
    parser.add_argument('--group', help='设置该参数表示组播，否则单播')
    parser.add_argument('--priority', type=int, default=5)
    args = parser.parse_args()
    client = Client(args.url, os.getenv('SDN_API_TOKEN', ''))
    print(client.register(dict(id=args.id, source=args.source, receivers=args.receivers,
        mbps=args.mbps, group=args.group, priority=args.priority,
        kind='multicast' if args.group else 'unicast', bidirectional=not bool(args.group))))
