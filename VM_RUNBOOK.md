# VirtualBox SDN 实验运行说明

部署与真实收包验证日期：2026-11-02。2026-11-03 已再次只读核验虚拟机与服务状态；下述状态不代表此后一直在线。

## 访问与当前环境

| 项目 | 地址或说明 |
| --- | --- |
| 虚拟机 | Ubuntu-SDN；UUID：16577e62-6c07-4266-a9ec-d5db8dca2b66 |
| 虚拟机项目目录 | `/home/sdn/sdn-brain-20261102-BnYxR4` |
| Windows 打开控制页面 | http://127.0.0.1:18000 （VirtualBox NAT 转发到虚拟机 8000） |
| Windows 访问 ONOS REST | http://127.0.0.1:18181/onos/v1/ （转发到虚拟机 8181） |
| 虚拟机内控制页面 | http://127.0.0.1:8000 |
| 控制器 | 复用已有 ONOS 2.7.0 容器 `onos`，OVS 使用 OpenFlow 1.3 |
| 应用 | Python、Sanic、requests；页面与服务均在虚拟机内运行 |

上述地址只供本机访问，不能作为公网网站地址。在 Windows 浏览器打开控制页面；需要访问令牌时，在页面“访问令牌”表单输入。请自行在虚拟机中查看项目目录下 `.vm.env` 的 `SDN_API_TOKEN` 配置项，不要公开或转发此文件。令牌保存在当前浏览器标签页的会话中，新标签页可能需要重新输入。

2026-11-03 核验：虚拟机正在运行；API 为 `mode=onos`、`connected=true`、`managed=true`，规则安装状态 `verified`、数量 11；识别到 6 台主机和 16 条有向链路，16 个负载样本均有效。两条通信需求各预留 5 Mbps，所有链路物理容量为 100 Mbps。

实验新增 `csdn_s1`～`csdn_s6` 六台交换机及 `csdn_h1`～`csdn_h6` 六台主机，包含 8 条物理链路。原有 5 台残留交换机被保留，因此 ONOS 可能显示 11 台设备；不能将其误认为实验包含 11 台交换机。主机地址为 `10.0.0.1`～`10.0.0.6`，每台接入对应编号交换机。

## 已验证范围与待完成事项

| 项目 | 结果 |
| --- | --- |
| 自动化测试 | 19 项通过 |
| 真实单播 | h2 → h6：5 次发送、5 次接收，丢包率 0% |
| 真实组播 | h1 → h4、h5、h6：三个接收端均收到数据，重复包为 0；多轮记录中曾各收到 834 包，最近保存的收包文件各为 830 包 |
| ONOS/OVS 规则下发 | 已实际安装并核验规则 |
| 根据真实负载自动改路 | **验收尚未通过**，不能据此宣称真实负载改路已验证 |

负载感知、路径计算及闭环下发已经实现。但 90 Mbps 背景流量发生器的实测流量不足以稳定触发验收条件；随后临时采用 1 Mbps 控制器预算的测试中，发送进程在确认改路前结束；清理阶段又遇到 ONOS HTTP 连接中断，后一个异常覆盖了前一个异常，因此没有得到完整的真实负载改路证据。测试后已经恢复 100 Mbps 容量与两条 5 Mbps 需求，并清除了临时背景规则。后续应在稳定 ONOS 连接及可控流量发生器下，补验“负载上升 → 路径改变 → 实际收包持续”的全过程。

实验使用已知主机与固定接入口进行登记；没有验证通用组播成员发现协议，也没有承诺硬实时或改路期间零丢包。

## 查看状态、日志和停止

以下命令在 **Ubuntu-SDN 终端**运行，无需重新部署：

```bash
sudo systemctl status codex-sdn-brain-20261102 codex-sdn-lab-20261102 --no-pager
sudo journalctl -u codex-sdn-brain-20261102 -n 100 --no-pager
sudo journalctl -u codex-sdn-lab-20261102 -n 100 --no-pager
sudo docker ps
sudo ovs-vsctl show
```

停止本实验：

```bash
sudo systemctl stop codex-sdn-brain-20261102
sudo systemctl stop codex-sdn-lab-20261102
```

拓扑程序收到正常停止信号后会清理其自身网络。脑服务单独停止时，已经下发的永久流表可能继续存在；需要清除时，应只删除本应用 `org.example.sdnbrain` 的规则，并核实对象范围。**不要运行 `mn -c`，不要批量删除交换机，也不要随意停止复用的 `onos` 容器**，以免影响原有环境。状态目录中的验收脚本失败记录不能替代上述服务健康检查。

## 虚拟机重启后的恢复

两个服务由 `systemd-run` 创建，属于临时单元；没有配置为开机自启。下面是依据实际部署命令整理的恢复步骤，**整机重启恢复尚未实际验收**。

1. 启动 Ubuntu-SDN，确认 VirtualBox NAT 仍保留 `18000 → 8000`、`18181 → 8181` 的转发。项目副本已保存在虚拟机磁盘中；只读共享目录重新挂载不是启动现有副本的前提。
2. 在虚拟机内启动依赖，确认 ONOS 就绪。如容器已运行，不必再次启动。

```bash
sudo systemctl start docker openvswitch-switch
sudo docker start onos
```

3. 先用上面的状态命令检查两个单元。仅在本实验确实停止、且没有残留 `csdn_` 交换机时，执行下面的启动命令。若存在残留，请先查日志，避免重复创建拓扑。

```bash
cd /home/sdn/sdn-brain-20261102-BnYxR4
sudo systemd-run --unit=codex-sdn-lab-20261102 --collect \
  --working-directory=/home/sdn/sdn-brain-20261102-BnYxR4 \
  /usr/bin/python3 /home/sdn/sdn-brain-20261102-BnYxR4/lab/topology.py \
  --headless --prefix csdn_ --dpid-base 0x53444e0000000000 \
  --state /run/codex-sdn-lab-20261102.json

sudo systemd-run --unit=codex-sdn-brain-20261102 --collect --uid=sdn \
  --working-directory=/home/sdn/sdn-brain-20261102-BnYxR4 \
  --property=EnvironmentFile=/home/sdn/sdn-brain-20261102-BnYxR4/.vm.env \
  /home/sdn/sdn-brain-20261102-BnYxR4/.venv/bin/python -m sdn.server
```

4. 打开控制页面，确认控制器连接、6 台主机、16 条有向链路与有效负载样本，再启用闭环并核对通信需求。若重启 ONOS 后主机不足，需要恢复实验主机登记或重新进行主机发现；不能仅凭网页能打开就认为转发恢复。原 ONOS 应用开关曾由部署脚本调整，变更前状态保存在项目内 `onos-apps-before.json`，不要盲目开启其他转发应用。

源码及进一步说明见 [项目 README](README.md) 与 [验证记录](VALIDATION.md)。
