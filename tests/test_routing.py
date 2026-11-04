import copy
import itertools
import math
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from sdn import demo
from sdn.routing import plan, validate_demand, shortest, multicast
from sdn.server import Brain, validate_batch
from sdn.onos import ONOS, APP_ID, fingerprint


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.t = demo.topology()
        self.ds = validate_batch(demo.demands())

    def test_shortest_and_reverse(self):
        d = validate_demand(dict(id='u', source='10.0.0.1', receivers=['10.0.0.6'], mbps=10))
        p = plan(self.t, [d])
        self.assertEqual(p['demands'][0]['cost'], 8)
        self.assertEqual(len(p['rules']), 8)

    def test_steiner_against_exhaustive_edges(self):
        root = self.t['devices'][0]['id']
        terminals = {self.t['devices'][i]['id'] for i in (3, 4, 5)} | {root}
        tree, algorithm = multicast(self.t['links'], root, terminals)
        physical = [e for e in self.t['links'] if e['src'] < e['dst']]
        best = math.inf
        for mask in range(1 << len(physical)):
            edges = [e for i, e in enumerate(physical) if mask & 1 << i]
            reached = {root}
            for _ in range(6):
                for e in edges:
                    if e['src'] in reached or e['dst'] in reached:
                        reached.update((e['src'], e['dst']))
            if terminals <= reached:
                best = min(best, sum(e['cost'] for e in edges))
        self.assertEqual(sum(e['cost'] for e in tree), best)
        self.assertIn('精确', algorithm)
        self.assertEqual(len({e['dst'] for e in tree}), len(tree))

    def test_multicast_shared_capacity(self):
        p = plan(self.t, self.ds[:1])
        for eid in p['demands'][0]['edges']:
            self.assertEqual(p['residual'][eid], 80)
        outputs = [len(r['treatment']['instructions']) for r in p['rules']]
        self.assertGreater(max(outputs), 1)
        self.assertTrue(all(r['selector']['criteria'][0]['type'] == 'IN_PORT' for r in p['rules']))

    def test_disconnect_rejects_entire_demand(self):
        for e in self.t['links']:
            if e['src'] == self.t['devices'][5]['id'] or e['dst'] == self.t['devices'][5]['id']:
                e['up'] = False
        p = plan(self.t, self.ds)
        self.assertTrue(all(d['status'] == 'rejected' for d in p['demands']))
        self.assertEqual(p['rules'], [])
        self.assertTrue(all(v == 100 for v in p['residual'].values()))

    def test_capacity_admission_and_no_partial_reverse_reservation(self):
        a = validate_demand(dict(id='a', source='10.0.0.1', receivers=['10.0.0.2'], mbps=60, priority=10))
        b = validate_demand(dict(id='b', source='10.0.0.1', receivers=['10.0.0.3'], mbps=60, priority=1))
        # Keep only s1<->s2<->s3, forcing demands to share s1->s2.
        allowed = {(1, 2), (2, 1), (2, 3), (3, 2)}
        self.t['links'] = [e for e in self.t['links'] if (int(e['src'][3:], 16), int(e['dst'][3:], 16)) in allowed]
        p = plan(self.t, [b, a])
        self.assertEqual([d['status'] for d in p['demands']], ['accepted', 'rejected'])
        self.assertTrue(all(v >= 0 for v in p['residual'].values()))
        self.t['links'] = [e for e in self.t['links'] if e['src'] < e['dst']]
        p = plan(self.t, [a])
        self.assertEqual(p['demands'][0]['status'], 'rejected')
        self.assertTrue(all(v == 100 for v in p['residual'].values()))

    def test_failure_reroutes(self):
        d = validate_demand(dict(id='u', source='10.0.0.1', receivers=['10.0.0.6'], mbps=1))
        before = plan(self.t, [d])['demands'][0]
        edge = before['edges'][0]
        self.t['links'] = [e for e in self.t['links'] if e['id'] != edge]
        after = plan(self.t, [d])['demands'][0]
        self.assertEqual(after['status'], 'accepted')
        self.assertNotIn(edge, after['edges'])

    def test_same_switch_and_multi_receivers(self):
        self.t['hosts'][1]['device'] = self.t['hosts'][0]['device']
        self.t['hosts'][1]['port'] = '99'
        d = validate_demand(dict(id='m', kind='multicast', source='10.0.0.1',
                                receivers=['10.0.0.2', '10.0.0.3'], mbps=1))
        p = plan(self.t, [d])
        root_rule = next(r for r in p['rules'] if r['deviceId'] == self.t['hosts'][0]['device'])
        self.assertIn({'type': 'OUTPUT', 'port': '99'}, root_rule['treatment']['instructions'])

    def test_directed_heuristic_no_cycles(self):
        links = [e for e in self.t['links'] if e['src'] < e['dst']]
        tree, algorithm = multicast(links, self.t['devices'][0]['id'], [d['id'] for d in self.t['devices'][3:]])
        self.assertIn('启发式', algorithm)
        self.assertEqual(len({e['dst'] for e in tree}), len(tree))

    def test_validation_conflicts_and_nan(self):
        with self.assertRaises(ValueError):
            validate_batch(self.ds + [dict(self.ds[0], id='duplicate')])
        for n in (float('nan'), -1, 0, float('inf')):
            with self.assertRaises(ValueError):
                validate_demand(dict(self.ds[0], mbps=n))

    def test_state_persistence_and_revision(self):
        scratch = Path(__file__).resolve().parent.parent / 'data'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as path:
            file = Path(path) / 'd.json'
            brain = Brain('demo', file)
            brain.update_demands([], brain.revision)
            self.assertEqual(Brain('demo', file).demands, [])
            with self.assertRaises(ValueError):
                brain.update_demands(self.ds, 1)

    def test_telemetry_only_counts_ingress(self):
        brain = Brain('demo', Path(tempfile.gettempdir()) / 'sdn-test-nonexistent-state.json')
        rules = plan(self.t, self.ds)['rules']
        flows = [dict(r, id=str(i), bytes=100) for i, r in enumerate(rules)]
        with patch('sdn.server.time.monotonic', side_effect=[10, 15]):
            brain.observe(flows, self.t)
            self.assertEqual(len(brain.telemetry), 3)  # One multicast, two unicast directions.
            self.assertTrue(all(t['mbps'] is None for t in brain.telemetry))
            brain.observe([dict(f, bytes=200) for f in flows], self.t)
            self.assertTrue(all(t['mbps'] == 0.00016 for t in brain.telemetry))


class ONOSTests(unittest.TestCase):
    def test_codec_normalization(self):
        rule = plan(demo.topology(), validate_batch(demo.demands()))['rules'][0]
        encoded = copy.deepcopy(rule)
        encoded['selector']['criteria'].reverse()
        for c in encoded['selector']['criteria']:
            if c['type'] == 'ETH_TYPE':
                c['ethType'] = '0x800'
            if c['type'] == 'IN_PORT':
                c['port'] = int(c['port'])
        self.assertEqual(fingerprint(rule), fingerprint(encoded))

    def test_reconcile_same_flow_id_treatment_update(self):
        desired = plan(demo.topology(), validate_batch(demo.demands()))['rules'][0]
        old = copy.deepcopy(desired)
        old['treatment']['instructions'] = [{'type': 'OUTPUT', 'port': '99'}]
        class Fake(ONOS):
            def __init__(self):
                self.entries = [dict(old, id='1', appId=APP_ID, state='ADDED'),
                                dict(old, id='2', appId='other.app', state='ADDED')]
                self.deleted = []
            def flows(self):
                return copy.deepcopy(self.entries)
            def request(self, method, path, body=None):
                if method == 'POST':
                    self.entries[0] = dict(body['flows'][0], id='1', appId=APP_ID, state='ADDED')
                if method == 'DELETE':
                    self.deleted.append(path)
                    self.entries = [e for e in self.entries if not path.endswith('/' + e['id'])]
        fake = Fake()
        self.assertEqual(fake.apply([desired])['state'], 'verified')
        self.assertEqual(fake.deleted, [])
        fake.apply([])
        self.assertEqual([e['appId'] for e in fake.entries], ['other.app'])


if __name__ == '__main__':
    unittest.main()
