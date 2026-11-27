# TC1 A1 免拆刷机脚本

斐讯 TC1 智能排插（A1 硬件版，EMW3031 / MK3031）免拆刷入社区固件的自动化脚本。

刷机原理、流程与全部固件文件均来自社区教程：
**[TC1 免拆刷机图文教程 — phicomm-io.github.io](https://phicomm-io.github.io/guides/tc1-no-disassembly-ethernet-wifi-ap.html)**

本仓库把教程中 Windows 下的手工步骤（Tftpd64、curl、netsh 等）整合成两个纯
Python 脚本，一次跑完「备份网络 → 配 TFTP → 发触发 → 等下载 → 恢复网络」，
不需要额外安装任何工具。

## 适用设备

- TC1 智能排插 **A1 硬件版**（EMW3031 / MK3031）
- 原厂固件 **1.0.0.26 / 1.0.0.24 / 1.0.0.21**（触发文件按版本自动匹配）
- TC1 A2、其他原厂版本、A1 空气净化器 **不适用**

## 与教程的对应关系

| 教程手工步骤 | 本脚本 |
|---|---|
| 备份网络配置、netsh 加 `10.0.0.2/8`、配防火墙 | 自动完成，结束时 `restore` 一键还原 |
| Tftpd64 提供 TFTP 下载 | 内置只读 TFTP（仅 `mico_ota.bin`、octet、拒绝写入） |
| curl 读 `/read-domain` 确认版本 | 自动读取并识别版本，交叉核对编译时间 |
| 按版本手选触发文件、手动发送 | 自动匹配 + 校验 payload 目标地址，**只发一次** |
| 盯 TFTP 日志等传完 | 自动等全部 1269 块传完再打印升级指引 |

## 文件清单

| 文件 | 用途 |
|---|---|
| `tc1_flash.py` | 交互式刷机向导（Windows，Python 3.8+，自动 UAC 提权） |
| `tc1_tftp_server.py` | 独立只读 TFTP 服务（跨平台备用；向导已内置同款，一般用不到） |
| `mico_ota.bin` | 0.9.6-ed4474 免拆过渡包，649,344 B，文件名不可改 |
| `tftp-trigger.bin` | 触发文件，适用 1.0.0.26，397 B |
| `tftp-trigger-1.0.0.24.bin` | 触发文件，适用 1.0.0.24，397 B |
| `tftp-trigger-1.0.0.21.bin` | 触发文件，适用 1.0.0.21，397 B |

固件与触发文件已随仓库附带（来源为教程下载页），可用教程提供的校验和核对。

## 前置条件

与原教程相同：

- 一台同时具备**有线 + 无线网卡**的电脑（USB 有线网卡亦可）和一根网线
- 路由器或 AP 提供 **2.4 GHz、无密码**热点 `PHICOMM_OTA_AP`，且无线与 LAN 互通
- 有线网卡接路由器 LAN 口；给排插稳定供电，刷机时拔掉排插上的用电设备

## 用法

> **重要：脚本内置的 UAC 自动提权不可信赖，请务必先手动打开管理员终端再运行。**
> 右键「开始」→「终端（管理员）」或「Windows PowerShell（管理员）」，
> `cd` 到本目录后再执行下面的命令。不要指望双击运行时的自动提权。

```bat
python tc1_flash.py             :: 交互式刷机向导
python tc1_flash.py tftp        :: 只运行内置 TFTP 服务（10.0.0.2:69）
python tc1_flash.py restore     :: 收尾：删 10.0.0.2/8、恢复 DHCP、清理防火墙规则
python tc1_flash.py --nic 以太网 :: 指定有线网卡
```

脚本给有线网卡添加 `10.0.0.2/8` 后，**请手动确认已生效**（把 `以太网`
换成实际网卡名）：

```bat
netsh interface ipv4 show addresses name=以太网
```

输出中应能看到 `10.0.0.2`。若自动添加失败或未生效，用下面的命令手动添加
（同样需要管理员终端）：

```bat
netsh interface ipv4 add address name=以太网 address=10.0.0.2 mask=255.0.0.0
```

网卡名可用 `netsh interface ipv4 show interfaces` 查询。

其他参数：`--timeout 秒`（等下载超时，默认 900）、`--no-pause`（结束不等回车）、
`--yes`（跳过确认，仅调试用）。

独立 TFTP 服务（备用，跨平台）：

```bat
python tc1_tftp_server.py --host 10.0.0.2
```

## 网络拓扑

| 连接 | 连到哪里 | 用途 |
|---|---|---|
| 电脑有线网卡 `10.0.0.2/8` | 路由器 / AP 的 LAN 口 | 提供 TFTP 固件下载 |
| 电脑无线网卡 | TC1 的 `Phicomm_TC1_XXXX` 热点 | 读版本、发触发请求 |
| TC1（收到请求后） | 路由器的 `PHICOMM_OTA_AP` 热点 | 从电脑 TFTP 拉固件 |

## 工作原理（简版）

1. 无线网卡连 TC1 配网热点，脚本读 `/read-domain` 识别原厂版本
2. 向原厂 OTA 接口发送对应版本的触发文件（payload 内含 `PHICOMM_OTA_AP`
   与 `10.0.0.2` TFTP 地址）
3. TC1 离开配网热点，转连 `PHICOMM_OTA_AP`，从 `10.0.0.2` 的 TFTP 拉取
   `mico_ota.bin`（0.9.6 过渡包）
4. 重启进入社区固件，再按教程升级到下载区最新兼容稳定版

## 注意事项

- `mico_ota.bin` 只是 **0.9.6 过渡包**，不随正式版更新；迁移成功后务必按教程
  末尾步骤升级到最新稳定版
- 触发文件**只发一次**；脚本会校验 payload 目标地址与版本匹配后再发
- 向导会临时修改网络配置（加 IP、加防火墙规则），结束记得用
  `python tc1_flash.py restore` 或按提示恢复
- 若系统里还开着 Tftpd64 会占用 UDP 69，脚本会报错提示关闭
- 刷机有风险，操作前请完整阅读原教程

## 致谢

- **[斐讯 IoTCloud 社区固件](https://phicomm-io.github.io/)** 及教程作者与
  维护者：免拆刷机原理、`mico_ota.bin` 过渡包、各版本触发文件、兼容性信息
  与图文教程全部来自该项目，本仓库只是按其流程做了自动化封装。

## License

[MIT](LICENSE) — 仅覆盖本仓库的脚本与文档；固件 `mico_ota.bin` 与触发文件的
权利归原社区项目所有，请按其站点条款使用。
