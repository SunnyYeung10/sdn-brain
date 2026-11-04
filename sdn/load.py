"""Measured directed egress load, excluding this app's observed traffic."""
from .onos import APP_ID


class LoadMonitor:
    def __init__(self, alpha=.3):
        self.alpha = alpha
        self.previous = {}
        self.smoothed = {}
        self.own_previous = {}

    def update(self, topology, statistics, flows, now, reservations=None):
        reservations = reservations or {}
        counters = {}
        for device in statistics['statistics']:
            for port in device['ports']:
                # ONOS port duration is the switch sample clock. Repeated REST
                # reads of the same sample must not fabricate zero-load samples.
                stamp = float(port['durationSec']) + float(port.get('durationNano', 0)) / 1e9
                counters[('port', device['device'], str(port['port']))] = (stamp, int(port['bytesSent']))
        own = {}
        for f in flows:
            if f.get('appId') != APP_ID or f.get('state') != 'ADDED':
                continue
            for a in f.get('treatment', {}).get('instructions', []):
                if a['type'] == 'OUTPUT':
                    p = (f['deviceId'], str(a['port']))
                    own.setdefault(p, {})[str(f['id'])] = int(f.get('bytes', 0))
        for edge in topology['links']:
            key = ('port', edge['src'], str(edge['src_port']))
            sample, old = counters.get(key), self.previous.get(key)
            stored = self.smoothed.get(key)
            if sample and old and sample[0] > old[0] and sample[1] >= old[1]:
                measured = (sample[1] - old[1]) * 8 / (sample[0] - old[0]) / 1e6
                current_own = own.get((edge['src'], str(edge['src_port'])), {})
                previous_own = self.own_previous.get(key)
                managed_rate = 0
                if previous_own and now > previous_own[0] and current_own.keys() == previous_own[1].keys() and all(current_own[k] >= previous_own[1][k] for k in current_own):
                    managed_rate = sum(current_own[k]-previous_own[1][k] for k in current_own)*8/(now-previous_own[0])/1e6
                self.own_previous[key] = (now, current_own)
                # Only subtract the portion already represented by declared
                # reservations. Excess managed traffic still counts as load.
                managed_rate = min(managed_rate, reservations.get(edge['id'], 0))
                background = max(0, measured - managed_rate)
                smooth = background if stored is None else self.alpha * background + (1-self.alpha) * stored[0]
                stored = (smooth, measured, now)
                self.smoothed[key] = stored
            elif sample and old and (sample[0] < old[0] or sample[1] < old[1]):
                self.smoothed.pop(key, None)
                stored = None
                self.own_previous.pop(key, None)
            elif sample and old is None:
                self.own_previous[key] = (now, own.get((edge['src'], str(edge['src_port'])), {}))
            valid = sample is not None and stored is not None and now - stored[2] <= 15
            cap = edge['capacity']
            edge.update(base_cost=edge['cost'], physical_capacity=cap, load_valid=valid)
            if valid:
                background, measured, _ = stored
                # 10% buckets reduce rerouting for small measurement jitter.
                utilization = min(1, background / cap)
                bucket = round(utilization * 10) / 10
                edge.update(measured_mbps=measured, background_mbps=background,
                            utilization=min(1, measured/cap),
                            capacity=max(0, cap-background),
                            cost=edge['cost'] * (1 + 4*bucket*bucket))
            else:
                edge.update(measured_mbps=None, background_mbps=None, utilization=None, up=False)
        self.previous = counters
        return topology
