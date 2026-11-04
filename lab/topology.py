#!/usr/bin/env python3
"""Linux only: sudo /usr/bin/python3 lab/topology.py --controller 127.0.0.1"""
import argparse
import time
import json
import signal
from pathlib import Path
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--controller', default='127.0.0.1')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--state', default='/run/codex-sdn-lab.json')
    parser.add_argument('--prefix', default='')
    parser.add_argument('--dpid-base', type=lambda x: int(x, 0), default=0)
    args = parser.parse_args()
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController('c0', controller=RemoteController, ip=args.controller, port=6653)
    switches = {i: net.addSwitch(f'{args.prefix}s{i}', dpid=f'{args.dpid_base+i:016x}', protocols='OpenFlow13', failMode='secure')
                for i in range(1, 7)}
    # Same link insertion order / port numbering as sdn.demo.topology.
    pairs = [(1, 2), (1, 3), (2, 3), (2, 4), (3, 5), (4, 5), (4, 6), (5, 6)]
    for a, b in pairs:
        net.addLink(switches[a], switches[b], bw=100)
    hosts = []
    for i in range(1, 7):
        h = net.addHost(f'{args.prefix}h{i}', ip=f'10.0.0.{i}/24', mac=f'00:00:00:00:00:{i:02x}')
        net.addLink(h, switches[i], bw=100)
        hosts.append(h)
    try:
        net.start()
        if not net.waitConnected(timeout=30):
            raise RuntimeError('OVS 未能在 30 秒内连接 ONOS；请检查 OpenFlow provider 和 6653 端口')
        net.staticArp()  # Avoid a separate ARP forwarding app on a cyclic fabric.
        for h in hosts:
            h.cmd(f'ip route replace 239.0.0.0/8 dev {h.defaultIntf()}')
        # Host provider learns ARP Packet-In without reactive forwarding.
        # Repeat after discovery so edge ports are classified correctly.
        for _ in range(3):
            for h in hosts:
                result = h.cmd(f'arping -U -c 1 -w 1 -I {h.defaultIntf()} {h.IP()}')
                if 'not found' in result:
                    raise RuntimeError('请安装 iputils-arping 后重新启动实验')
            time.sleep(2)
        print('六交换机环路拓扑已启动。先在控制台注册需求并启用闭环，再测试对应通信。')
        print('故障测试：link s3 s5 down；恢复：link s3 s5 up')
        print('重新发现主机：h1 arping -U -c 2 -I h1-eth0 10.0.0.1')
        if args.headless:
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            Path(args.state).write_text(json.dumps({f'h{i}': {'pid': h.pid, 'ip': h.IP(), 'name': h.name} for i,h in enumerate(hosts,1)}))
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            CLI(net)
    finally:
        net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
