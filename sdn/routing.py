"""Dependency-free routing; all capacities in Mbit/s, positive link costs."""
import heapq
import itertools
import ipaddress
import math


def validate_demand(d):
    d = dict(d)
    d['id'] = str(d.get('id', '')).strip()
    if not d['id'] or len(d['id']) > 64:
        raise ValueError('需求 ID 长度应为 1–64')
    d['kind'] = d.get('kind', 'unicast')
    if d['kind'] not in ('unicast', 'multicast'):
        raise ValueError('未知需求类型')
    d['source'] = str(ipaddress.IPv4Address(d['source']))
    if ipaddress.ip_address(d['source']).is_multicast:
        raise ValueError('源地址不能是组播地址')
    d['receivers'] = sorted(set(str(ipaddress.IPv4Address(x)) for x in d['receivers']))
    if not d['receivers'] or d['source'] in d['receivers']:
        raise ValueError('接收端不能为空，且不能包含源端')
    if any(ipaddress.ip_address(x).is_multicast for x in d['receivers']):
        raise ValueError('接收端应填写主机单播 IP')
    if d['kind'] == 'unicast' and len(d['receivers']) != 1:
        raise ValueError('单播需求应只有一个接收端')
    d['mbps'] = float(d.get('mbps', 1))
    if not math.isfinite(d['mbps']) or not 0 < d['mbps'] <= 1000000:
        raise ValueError('带宽必须为正数且不超过 1000000 Mbps')
    d['priority'] = int(d.get('priority', 1))
    if not 0 <= d['priority'] <= 100:
        raise ValueError('调度优先级范围为 0–100')
    d['bidirectional'] = bool(d.get('bidirectional', True)) if d['kind'] == 'unicast' else False
    if d['kind'] == 'multicast':
        d['group'] = str(ipaddress.IPv4Address(d.get('group', '239.1.1.1')))
        if not ipaddress.ip_address(d['group']) in ipaddress.ip_network('239.0.0.0/8'):
            raise ValueError('实验组播地址应位于 239.0.0.0/8')
    return d


def shortest(links, source, target):
    if source == target:
        return []
    adj = {}
    for e in links:
        adj.setdefault(e['src'], []).append(e)
    queue, dist, prev = [(0, source)], {source: 0}, {}
    while queue:
        cost, u = heapq.heappop(queue)
        if cost != dist[u]:
            continue
        if u == target:
            result = []
            while u != source:
                edge = prev[u]
                result.append(edge)
                u = edge['src']
            return result[::-1]
        for e in adj.get(u, []):
            v, nc = e['dst'], cost + e['cost']
            if nc < dist.get(v, math.inf):
                dist[v], prev[v] = nc, e
                heapq.heappush(queue, (nc, v))
    raise ValueError('无可用路径，或剩余带宽不足')


def multicast(links, root, terminals):
    """Exact minimum-cost undirected Steiner tree with <=12 optional vertices.

    Enumerate optional vertex sets, compute induced MST, retain cheapest connected
    candidate. Symmetric graph required; parallel links kept by physical ports.
    For large graphs / asymmetric graphs use directed incremental attachment.
    """
    terminals = set(terminals) | {root}
    bykey = {(e['src'], e['src_port'], e['dst'], e['dst_port']): e for e in links}
    symmetric = all((e['dst'], e['dst_port'], e['src'], e['src_port']) in bykey
                    and bykey[e['dst'], e['dst_port'], e['src'], e['src_port']]['cost'] == e['cost']
                    for e in links)
    nodes = {e['src'] for e in links} | {e['dst'] for e in links} | terminals
    optional = sorted(nodes - terminals)
    if symmetric and len(optional) <= 12 and len(nodes) <= 20:
        edges = sorted([e for e in links if e['src'] < e['dst']], key=lambda e: e['cost'])
        best, best_cost = None, math.inf
        for mask in range(1 << len(optional)):
            vertices = terminals | {v for i, v in enumerate(optional) if mask & (1 << i)}
            parent = {v: v for v in vertices}
            def find(v):
                while parent[v] != v:
                    parent[v] = parent[parent[v]]
                    v = parent[v]
                return v
            tree, cost = [], 0
            for e in edges:
                u, v = e['src'], e['dst']
                if u not in vertices or v not in vertices:
                    continue
                a, b = find(u), find(v)
                if a != b:
                    parent[a] = b
                    tree.append(e)
                    cost += e['cost']
            if len(tree) == len(vertices) - 1 and cost < best_cost:
                best, best_cost = tree, cost
        if best is None:
            raise ValueError('组播接收端不可达，或剩余带宽不足')
        # Root the undirected tree and remove non-terminal leaves.
        adj = {}
        for e in best:
            reverse = bykey[e['dst'], e['dst_port'], e['src'], e['src_port']]
            adj.setdefault(e['src'], []).append(e)
            adj.setdefault(e['dst'], []).append(reverse)
        def visit(u, p):
            result = []
            for e in adj.get(u, []):
                if e['dst'] != p:
                    children = visit(e['dst'], u)
                    if e['dst'] in terminals or children:
                        result += [e] + children
            return result
        return visit(root, None), '精确 Steiner（对称链路）'
    # Attach reachable terminals to the existing rooted tree. Do not permit an
    # edge entering an existing tree vertex: avoids merging paths and duplicates.
    reached, pending, tree = {root}, terminals - {root}, []
    while pending:
        candidates = []
        available = [e for e in links if e['dst'] not in reached]
        for start, target in itertools.product(sorted(reached), sorted(pending)):
            try:
                path = shortest(available, start, target)
                candidates.append((sum(e['cost'] for e in path), target, path))
            except ValueError:
                pass
        if not candidates:
            raise ValueError('组播接收端不可达（近似算法未找到可行树）')
        _, _, path = min(candidates, key=lambda x: (x[0], x[1]))
        tree += path
        reached.update(e['dst'] for e in path)
        pending -= reached
    return tree, '增量最短路径树（启发式）'


def rule(device, in_port, outputs, source, destination):
    return {'deviceId': device, 'priority': 40000, 'isPermanent': True,
            'timeout': 0, 'tableId': 0,
            'selector': {'criteria': [
                {'type': 'IN_PORT', 'port': str(in_port)},
                {'type': 'ETH_TYPE', 'ethType': '0x0800'},
                {'type': 'IPV4_SRC', 'ip': source + '/32'},
                {'type': 'IPV4_DST', 'ip': destination + '/32'}]},
            'treatment': {'instructions': [{'type': 'OUTPUT', 'port': str(p)}
                                           for p in sorted(set(outputs), key=str)]}}


def compile_tree(edges, src, receivers, destination):
    incoming = {src['device']: src['port']}
    outputs = {}
    for e in edges:
        incoming[e['dst']] = e['dst_port']
        outputs.setdefault(e['src'], []).append(e['src_port'])
    for h in receivers:
        outputs.setdefault(h['device'], []).append(h['port'])
    # Depth orders the source last, making initial installation downstream-first.
    depth = {src['device']: 0}
    remaining = list(edges)
    while remaining:
        progress = [e for e in remaining if e['src'] in depth]
        if not progress:
            raise ValueError('转发树无法从源端遍历')
        for e in progress:
            depth[e['dst']] = depth[e['src']] + 1
            remaining.remove(e)
    return [rule(u, incoming[u], outputs[u], src['ip'], destination)
            for u in sorted(outputs, key=lambda u: -depth[u])]


def plan(topology, demands):
    hosts = {}
    for h in topology['hosts']:
        if h['ip'] in hosts:
            raise ValueError('存在重复主机 IP，无法唯一定位')
        hosts[h['ip']] = h
    links = [e for e in topology['links'] if e.get('up', True)]
    residual = {e['id']: e['capacity'] for e in links}
    results, all_rules = [], []
    for d in sorted(demands, key=lambda x: (-x['priority'], -x['mbps'], x['id'])):
        item = {'id': d['id'], 'status': 'rejected', 'edges': [], 'rules': []}
        try:
            src = hosts[d['source']]
            receivers = [hosts[ip] for ip in d['receivers']]
            available = [e for e in links if residual[e['id']] + 1e-9 >= d['mbps']]
            if d['kind'] == 'unicast':
                forward = shortest(available, src['device'], receivers[0]['device'])
                reverse = shortest(available, receivers[0]['device'], src['device']) if d['bidirectional'] else []
                edges = forward + reverse
                rules = compile_tree(forward, src, receivers, receivers[0]['ip'])
                if d['bidirectional']:
                    rules += compile_tree(reverse, receivers[0], [src], src['ip'])
                algorithm = 'Dijkstra / 有向链路'
            else:
                # Exact solver needs both directions to have capacity. If the
                # filtered graph is asymmetric, the directed heuristic is used.
                edges, algorithm = multicast(available, src['device'], [h['device'] for h in receivers])
                rules = compile_tree(edges, src, receivers, d['group'])
            # One reservation per directed tree edge: multicast sharing is real.
            for eid in {e['id'] for e in edges}:
                residual[eid] -= d['mbps']
            item.update(status='accepted', edges=[e['id'] for e in edges], rules=rules,
                        cost=sum(e['cost'] for e in edges), algorithm=algorithm)
            all_rules += rules
        except KeyError:
            item['reason'] = '主机尚未被 ONOS 发现，或已离线'
        except ValueError as exc:
            item['reason'] = str(exc)
        results.append(item)
    return {'demands': results, 'rules': all_rules, 'residual': residual,
            'strategy': '按优先级、带宽降序逐个准入；不保证多需求联合全局最优'}
