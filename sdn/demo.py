def topology():
    device = lambda i: 'of:' + format(i, '016x')
    pairs = [(1, 2, 1), (1, 3, 2), (2, 3, 1), (2, 4, 2),
             (3, 5, 1), (4, 5, 1), (4, 6, 1), (5, 6, 2)]
    ports = {i: 1 for i in range(1, 7)}
    links = []
    for a, b, cost in pairs:
        pa, pb = ports[a], ports[b]
        ports[a] += 1
        ports[b] += 1
        for u, v, up, vp in [(a, b, pa, pb), (b, a, pb, pa)]:
            links.append({'id': f'{device(u)}/{up}>{device(v)}/{vp}',
                          'src': device(u), 'dst': device(v), 'src_port': str(up),
                          'dst_port': str(vp), 'cost': cost, 'capacity': 100.0, 'up': True})
    return {'devices': [{'id': device(i), 'label': f's{i}'} for i in range(1, 7)],
            'links': links,
            'hosts': [{'id': f'00:00:00:00:00:{i:02x}/None', 'ip': f'10.0.0.{i}',
                       'device': device(i), 'port': str(ports[i]), 'label': f'h{i}'}
                      for i in range(1, 7)]}


def demands():
    return [dict(id='视频分发', kind='multicast', source='10.0.0.1',
                 receivers=['10.0.0.4', '10.0.0.5', '10.0.0.6'],
                 group='239.1.1.1', mbps=20, priority=10),
            dict(id='业务 A', kind='unicast', source='10.0.0.2',
                 receivers=['10.0.0.6'], mbps=15, priority=5, bidirectional=True)]
