#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TC1 A1 免拆刷机向导（Windows / Python 3.8+）。

一次跑完三件事：
  1. 刷机前：备份网络配置 → 给有线网卡加 10.0.0.2/8 → 内置 TFTP 提供 mico_ota.bin
  2. 刷机中：连接 TC1 配网热点 → 读 read-domain 识别版本 → 匹配触发文件 → 只发一次
  3. 刷机后：等待设备下载并重启 → 给出升级提示 → 可选恢复网络

用法：
  python tc1_flash.py             # 交互式刷机向导（自动 UAC 提权）
  python tc1_flash.py restore     # 收尾：删 10.0.0.2/8、恢复 DHCP、清理防火墙规则
  python tc1_flash.py tftp        # 只运行内置 TFTP 服务（10.0.0.2:69）

参数：
  --nic 名称     指定有线网卡（默认读 nic-name.txt，否则「以太网」）
  --timeout 秒   等待设备下载的超时（默认 900）
  --yes          跳过交互确认（危险，仅调试用）
  --no-pause     结束时不等回车
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import http.client
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIRMWARE_FILE = ROOT / "mico_ota.bin"
BACKUP_FILE = ROOT / "netbackup.txt"
NIC_FILE = ROOT / "nic-name.txt"
VERSION_FILE = ROOT / "read-domain.txt"

TFTP_HOST = "10.0.0.2"
TFTP_PORT = 69
BLOCK_SIZE = 512
BLOCK_TIMEOUT = 3.0
BLOCK_RETRIES = 5
PROGRESS_EVERY = 100

DEVICE_HOST = "10.10.10.1"
DEVICE_PORT = 8000

MRVL_MAGIC = b"MRVL"
SLOT_LIMIT = 0xA9000  # 692224 B，应用槽上限
SNAPSHOT_MD5 = "137bbe25d40f0a45b2b3129f035ea4c3"

# 版本 -> (触发文件名, payload 目标地址)
TRIGGERS = {
    "1.0.0.26": ("tftp-trigger.bin", 0x1F073D9F),
    "1.0.0.24": ("tftp-trigger-1.0.0.24.bin", 0x1F073D63),
    "1.0.0.21": ("tftp-trigger-1.0.0.21.bin", 0x1F073D2F),
}
TRIGGER_SIZE = 397
TRIGGER_PAD = 392

# 教程记录的编译时间，用于交叉核对（不一致只告警，不直接拦）
COMPILE_TIMES = {
    "1.0.0.26": "Apr 13 2018 10:09:06",
    "1.0.0.24": "Apr 13 2018 10:09:06",
    "1.0.0.21": "Oct 29 2017 11:09:21",
}

FIREWALL_RULE = "TC1 OTA TFTP (Python)"


# ---------------------------------------------------------------- 输出与交互
def hr(char="-", width=62):
    print(char * width, flush=True)


def info(message):
    print(f"[..] {message}", flush=True)


def ok(message):
    print(f"[OK] {message}", flush=True)


def warn(message):
    print(f"[!!] {message}", flush=True)


def err(message):
    print(f"[停止] {message}", flush=True)


def step(index, total, title):
    print()
    hr("=")
    print(f" 第 {index}/{total} 步  {title}")
    hr("=")


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def ask(prompt, default=""):
    try:
        raw = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130)
    raw = raw.strip()
    return raw if raw else default


def confirm(prompt, expected="YES"):
    return ask(prompt).upper() == expected.upper()


def setup_console():
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("TC1 免拆刷机")
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def decode_bytes(data):
    """netsh 输出是 UTF-8、PowerShell 输出是 ANSI 代码页，两种都要认。"""
    for encoding in ("utf-8", "cp936", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp936", "replace")


def run(command, timeout=60):
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, decode_bytes(proc.stdout)


# ---------------------------------------------------------------- 权限
def is_admin():
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def require_admin():
    """改 IP / 防火墙需要管理员；不是就重新以管理员身份拉起自己。"""
    if is_admin():
        return
    info("需要管理员权限（改 IP、加防火墙规则），正在请求 UAC 提权 …")
    params = subprocess.list2cmdline([str(Path(__file__).resolve())] + sys.argv[1:])
    try:
        code = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, str(ROOT), 1
        )
    except Exception as exc:
        err(f"提权失败：{exc}")
        sys.exit(1)
    if code <= 32:
        err("提权被取消或失败，请右键「以管理员身份运行」。")
        sys.exit(1)
    sys.exit(0)


# ---------------------------------------------------------------- 网卡与 IP
def show_interfaces():
    _, out = run(["netsh", "interface", "ipv4", "show", "interfaces"])
    print(out.rstrip())


def interface_config(nic):
    return run(["netsh", "interface", "ipv4", "show", "config", f"name={nic}"])


def interface_exists(nic):
    code, out = interface_config(nic)
    return code == 0 and nic in out


def has_ipv4(nic, address):
    code, out = run(["netsh", "interface", "ipv4", "show", "addresses", f"name={nic}"])
    if code != 0:
        return False
    return re.search(r"(?<![\d.])" + re.escape(address) + r"(?![\d.])", out) is not None


def add_ipv4(nic, address, mask):
    return run(
        ["netsh", "interface", "ipv4", "add", "address",
         f"name={nic}", f"address={address}", f"mask={mask}"]
    )


def delete_ipv4(nic, address):
    return run(
        ["netsh", "interface", "ipv4", "delete", "address",
         f"name={nic}", f"address={address}"]
    )


def set_dhcp(nic):
    run(["netsh", "interface", "ipv4", "set", "address", f"name={nic}", "source=dhcp"])
    run(["netsh", "interface", "ipv4", "set", "dnsservers", f"name={nic}", "source=dhcp"])
    run(["ipconfig", "/renew", nic], timeout=120)


def find_config_mode_adapter():
    """返回 (网卡名, 10.10.10.x 地址)，没找到返回 None。"""
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "ForEach-Object { $_.InterfaceAlias + '|' + $_.IPAddress }"
    )
    code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30)
    if code != 0:
        return None
    for line in out.splitlines():
        alias, _, address = line.partition("|")
        if address.startswith("10.10.10."):
            return alias.strip(), address.strip()
    return None


def allow_tftp_firewall():
    """放行本 python.exe 的 UDP 入站：TFTP 数据端口是随机的，只开 69 不够。"""
    run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])
    return run(
        ["netsh", "advfirewall", "firewall", "add", "rule",
         f"name={FIREWALL_RULE}", "dir=in", "action=allow",
         "protocol=UDP", f"program={sys.executable}"]
    )


def remove_firewall_rule():
    return run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])


# ---------------------------------------------------------------- 设备 HTTP
def http_call(method, path, body=None, timeout=8.0):
    """直连 10.10.10.1，不经系统代理。"""
    conn = http.client.HTTPConnection(DEVICE_HOST, DEVICE_PORT, timeout=timeout)
    try:
        conn.request(
            method, path, body=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def read_device_info():
    """读 /read-domain，失败可重试；返回响应文本或 None。"""
    while True:
        info(f"读取 http://{DEVICE_HOST}:{DEVICE_PORT}/read-domain …")
        try:
            status, payload = http_call("GET", "/read-domain", timeout=8)
        except Exception as exc:
            warn(f"读取失败：{exc.__class__.__name__}: {exc}")
            answer = ask("检查无线是否连着 Phicomm_TC1_XXXX、是否拿到 10.10.10.x。"
                         "输入 R 重试，直接回车退出: ")
            if answer.upper() == "R":
                continue
            return None
        VERSION_FILE.write_bytes(payload)
        text = decode_bytes(payload)
        print(text.rstrip())
        if status != 200:
            warn(f"HTTP {status}：设备可能还没进入配网模式")
            answer = ask("输入 R 重试，直接回车退出: ")
            if answer.upper() == "R":
                continue
            return None
        return text


def detect_version(text):
    match = re.search(r'VERSION"?\s*[:=]\s*"?1\.0\.0\.(21|24|26)\b', text)
    if match is None:
        match = re.search(r"1\.0\.0\.(21|24|26)\b", text)
    return f"1.0.0.{match.group(1)}" if match else None


def detect_compile_time(text):
    match = re.search(r'COMPILE_TIME"?\s*[:=]\s*"([^"]*)"', text)
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------- 本地文件
def load_firmware():
    if not FIRMWARE_FILE.is_file():
        err(f"未找到 {FIRMWARE_FILE.name}，请把它放到脚本同目录")
        return None
    data = FIRMWARE_FILE.read_bytes()
    if len(data) < 32:
        err("mico_ota.bin 太小，不是有效的应用槽镜像")
        return None
    if not data.startswith(MRVL_MAGIC):
        err("mico_ota.bin 缺少 MRVL 标记：需要「原始应用槽镜像 + 16 字节 MD5」，"
            "不能是整片备份、完整 .all.bin 或带 PHOTA001 包头的网页升级包")
        return None
    if len(data) > SLOT_LIMIT:
        err(f"mico_ota.bin 为 {len(data)} B，超出 {SLOT_LIMIT} B 的应用槽上限")
        return None
    image, digest = data[:-16], data[-16:]
    if hashlib.md5(image).digest() != digest:
        err("mico_ota.bin 末尾 16 字节 MD5 与镜像内容不匹配，设备会拒绝切槽")
        return None
    actual = hashlib.md5(image).hexdigest()
    ok(f"{FIRMWARE_FILE.name} {len(data):,} B，MRVL 镜像 + MD5 校验通过")
    if actual == SNAPSHOT_MD5:
        ok("匹配配套 0.9.6-ed4474 过渡包快照")
    else:
        warn(f"镜像 MD5 为 {actual}，不是配套快照（自定义镜像请自行确认适配本硬件）")
    return data


def load_trigger(version):
    name, address = TRIGGERS[version]
    path = ROOT / name
    if not path.is_file():
        err(f"缺少触发文件：{name}")
        return None
    data = path.read_bytes()
    if len(data) != TRIGGER_SIZE:
        err(f"{name} 为 {len(data)} B，应为 {TRIGGER_SIZE} B")
        return None
    if data[:TRIGGER_PAD] != b"A" * TRIGGER_PAD:
        err(f"{name} 的填充区不是 {TRIGGER_PAD} 个 A，文件可能被改动")
        return None
    if data[TRIGGER_PAD:TRIGGER_PAD + 4] != address.to_bytes(4, "little"):
        err(f"{name} 的目标地址不是 0x{address:08X}（小端），与 {version} 不匹配")
        return None
    if data[TRIGGER_PAD + 4:] != b"\0":
        err(f"{name} 结尾不是单个 NUL 字节")
        return None
    return data


def read_nic_name():
    try:
        raw = NIC_FILE.read_bytes()
    except OSError:
        return None
    return decode_bytes(raw).strip() or None


# ---------------------------------------------------------------- TFTP 服务
class TftpServer(threading.Thread):
    """只读 TFTP：只提供 mico_ota.bin，octet 模式，拒绝写入。"""

    def __init__(self, host, firmware, filename="mico_ota.bin"):
        super().__init__(daemon=True, name="tftp")
        self.host = host
        self.firmware = firmware
        self.filename = filename.encode()
        self.stop_event = threading.Event()
        self.ready = threading.Event()  # 已绑定端口，可以接收 RRQ
        self.finished = threading.Event()  # 一次传输结束（成功或失败）
        self.failed = None
        self.client = None

    def stop(self):
        self.stop_event.set()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for attempt in range(1, 6):
                try:
                    sock.bind((self.host, TFTP_PORT))
                    break
                except OSError as exc:
                    if exc.errno in (errno.EADDRNOTAVAIL, 10049) and attempt < 5:
                        time.sleep(0.6)  # 地址刚加上，等内核生效
                        continue
                    raise
            sock.settimeout(0.5)
        except OSError as exc:
            sock.close()
            if exc.errno in (errno.EADDRINUSE, 10048):
                self.failed = f"UDP {TFTP_PORT} 已被占用（Tftpd64 是否还在运行？）：{exc}"
            elif exc.errno in (errno.EADDRNOTAVAIL, 10049):
                self.failed = f"{self.host} 没有配置到有线网卡：{exc}"
            else:
                self.failed = f"无法绑定 UDP {self.host}:{TFTP_PORT}：{exc}"
            log(f"启动失败：{self.failed}")
            self.finished.set()
            return

        self.ready.set()
        log(f"TFTP 就绪：UDP {self.host}:{TFTP_PORT}，文件 {self.filename.decode()}，"
            f"{len(self.firmware)} B")
        log("等待设备发起 RRQ（读请求）…")
        try:
            while not self.stop_event.is_set():
                try:
                    packet, peer = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._handle_packet(sock, packet, peer)
        finally:
            sock.close()

    def _handle_packet(self, sock, packet, peer):
        if len(packet) < 4:
            self._send_error(sock, peer, 4, b"malformed request")
            return
        opcode = struct.unpack("!H", packet[:2])[0]
        fields = packet[2:].split(b"\0")
        if opcode != 1:
            self._send_error(sock, peer, 4, b"read requests only")
            return
        if len(fields) < 2 or not fields[0] or not fields[1]:
            self._send_error(sock, peer, 4, b"malformed request")
            return
        name, mode = fields[0], fields[1].lower()
        if name != self.filename or mode != b"octet":
            log(f"拒绝 {peer[0]}:{peer[1]} 的请求：filename={name!r} mode={mode!r}")
            self._send_error(sock, peer, 1, b"only mico_ota.bin octet")
            return
        if self.finished.is_set():
            log(f"又收到 {peer[0]}:{peer[1]} 的 RRQ，重新传输一遍")
        self._transfer(peer)

    @staticmethod
    def _send_error(sock, peer, code, message):
        sock.sendto(struct.pack("!HH", 5, code) + message + b"\0", peer)

    def _transfer(self, peer):
        # 末尾正好整块时 TFTP 还要求补一个空块，所以统一 +1
        total = len(self.firmware) // BLOCK_SIZE + 1
        log(f"开始传输 → {peer[0]}:{peer[1]}，共 {total} 块")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
            try:
                stream.bind((self.host, 0))
            except OSError as exc:
                self.failed = f"无法创建传输套接字：{exc}"
                log(f"传输失败：{self.failed}")
                self.finished.set()
                return
            stream.settimeout(BLOCK_TIMEOUT)

            for block in range(1, total + 1):
                if self.stop_event.is_set():
                    self.failed = "服务被手动停止"
                    self.finished.set()
                    return
                offset = (block - 1) * BLOCK_SIZE
                packet = struct.pack("!HH", 3, block) + self.firmware[offset:offset + BLOCK_SIZE]
                for attempt in range(1, BLOCK_RETRIES + 1):
                    stream.sendto(packet, peer)
                    try:
                        ack, sender = stream.recvfrom(516)
                    except socket.timeout:
                        log(f"第 {block}/{total} 块 ACK 超时（{attempt}/{BLOCK_RETRIES}）")
                        continue
                    if sender == peer and ack == struct.pack("!HH", 4, block):
                        break
                else:
                    self.failed = f"第 {block}/{total} 块始终没有被确认"
                    log(f"传输失败：{self.failed}")
                    self.finished.set()
                    return

                if block in (1, total) or block % PROGRESS_EVERY == 0:
                    sent = min(block * BLOCK_SIZE, len(self.firmware))
                    log(f"进度：第 {block}/{total} 块，{sent}/{len(self.firmware)} B")

        self.client = peer
        log(f"传输完成：{len(self.firmware)} B → {peer[0]}:{peer[1]}")
        log("设备正在校验 MD5、切换应用槽并重启，请保持供电 …")
        self.finished.set()


def wait_ready(server, timeout=6.0):
    """等服务线程绑定好端口；绑定失败会立刻返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.ready.is_set():
            return True
        if server.failed:
            return False
        time.sleep(0.05)
    return server.ready.is_set()


def wait_for_transfer(server, timeout):
    start = time.monotonic()
    next_note = start + 20
    while not server.finished.wait(0.5):
        now = time.monotonic()
        if now - start >= timeout:
            return False
        if now >= next_note:
            log(f"仍在等待设备连接 TFTP（已等 {int(now - start)} s）…")
            next_note = now + 20
    return server.failed is None


# ---------------------------------------------------------------- 恢复网络
def do_restore(nic):
    if has_ipv4(nic, TFTP_HOST):
        code, out = delete_ipv4(nic, TFTP_HOST)
        if code == 0:
            ok(f"已从 {nic} 删除 {TFTP_HOST}/8")
        else:
            warn(f"删除 {TFTP_HOST} 失败：{out.strip()}")
    else:
        info(f"{nic} 上本来就没有 {TFTP_HOST}")

    info("恢复 DHCP 自动获取并续租 …")
    set_dhcp(nic)
    code, _ = remove_firewall_rule()
    if code == 0:
        ok(f"已删除防火墙规则「{FIREWALL_RULE}」")

    print()
    hr()
    _, config = interface_config(nic)
    print(config.rstrip())
    hr()
    print("""  提醒：
  - 关闭 TFTP 窗口；把路由器 PHICOMM_OTA_AP 恢复成原来的 WiFi 设置
  - 若刷机前是静态 IP，按 netbackup.txt 手动恢复，不要停在 DHCP
  - 检查并撤销本次新增的防火墙放行（脚本已尝试删除）""")


# ---------------------------------------------------------------- 三个命令
def banner():
    print("=" * 62)
    print("  TC1 A1 免拆刷机向导（Python 版）")
    print(f"  工作目录：{ROOT}")
    print("  仅适用：A1 硬件 + 原厂 1.0.0.26 / 1.0.0.24 / 1.0.0.21")
    print("=" * 62)


def cmd_flash(args):
    if os.name != "nt":
        err("自动配置 IP / 防火墙依赖 Windows netsh；macOS / Linux 请按教程手动操作。")
        return 2
    require_admin()
    banner()
    print("""
  开始前确认：排插上不要接负载、全程稳定供电、失败按有线恢复流程处理。
""")

    def hint_restore():
        warn(f"网络仍是刷机配置（{TFTP_HOST}/8 + 防火墙规则「{FIREWALL_RULE}」），"
             "收尾请运行：python tc1_flash.py restore")

    step(1, 8, "检查固件与触发文件")
    firmware = load_firmware()
    if firmware is None:
        return 1
    missing = [name for name, _ in TRIGGERS.values() if not (ROOT / name).is_file()]
    if missing:
        warn(f"缺少触发文件：{', '.join(missing)}（只有对应版本才需要）")
    else:
        ok("三个版本的触发文件齐全")

    step(2, 8, "选择有线网卡")
    nic = args.nic or read_nic_name() or "以太网"
    if not args.nic:
        show_interfaces()
        nic = ask(f"接路由器 LAN 口的有线网卡名 [默认 {nic}]: ", nic)
    if not interface_exists(nic):
        err(f"找不到网卡「{nic}」，请从上面的列表里重新选")
        return 1
    NIC_FILE.write_text(nic + "\n", encoding="utf-8")
    ok(f"使用网卡：{nic}")

    step(3, 8, "备份当前网络配置")
    _, all_config = run(["ipconfig", "/all"], timeout=60)
    _, nic_config = interface_config(nic)
    BACKUP_FILE.write_text(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 刷机前网络配置（网卡：{nic}）\n\n"
        f"{all_config}\n{nic_config}\n",
        encoding="utf-8",
    )
    ok(f"已保存到 {BACKUP_FILE.name}")

    step(4, 8, "添加 10.0.0.2/8")
    if has_ipv4(nic, TFTP_HOST):
        ok(f"{TFTP_HOST}/8 已存在，跳过")
    else:
        code, out = add_ipv4(nic, TFTP_HOST, "255.0.0.0")
        if code != 0:
            err(f"添加失败：{out.strip()}")
            return 1
        if not has_ipv4(nic, TFTP_HOST):
            err("添加后仍未检测到 10.0.0.2/8，请检查网卡名、地址冲突")
            return 1
        ok(f"{TFTP_HOST}/8 已加到 {nic}")

    step(5, 8, "放行防火墙并启动内置 TFTP")
    added, out = allow_tftp_firewall()
    if added:
        ok(f"已添加防火墙规则「{FIREWALL_RULE}」（仅放行本 python 的 UDP 入站）")
    else:
        warn(f"防火墙规则添加失败，设备可能拿不到文件：{out.strip()}")

    server = TftpServer(TFTP_HOST, firmware)
    server.start()
    if not wait_ready(server):
        err(server.failed or "TFTP 服务未能在 6 s 内就绪")
        err(f"确认「{nic}」已连路由器 LAN 口、10.0.0.2/8 已生效、UDP 69 没被占用")
        hint_restore()
        return 1

    step(6, 8, "让 TC1 进入配网模式并读取原厂版本")
    print("""  1. 拔掉排插上的负载，保持稳定供电
  2. TC1 断电重新上电，上电 3 秒内双击配网键（LED 约 500 ms 慢闪）
  3. 电脑无线连上开放热点 Phicomm_TC1_XXXX（提示无 Internet 属正常）
  备用手势：长按配网键到快闪 → 保持一会 → 松手 → 快速短按两次""")
    ask("无线已连上 TC1 配网热点后按回车 …")

    found = find_config_mode_adapter()
    if found:
        ok(f"检测到 {found[0]} 获得 {found[1]}，可访问 {DEVICE_HOST}")
    else:
        warn("没有网卡拿到 10.10.10.x 地址，请确认无线真的连上了 TC1 热点")

    text = read_device_info()
    if text is None:
        hint_restore()
        return 1
    version = detect_version(text)
    if version is None:
        err("没能识别 VERSION：只支持 1.0.0.26 / 1.0.0.24 / 1.0.0.21，其他版本不要继续")
        hint_restore()
        return 1
    ok(f"原厂版本：{version}")

    compile_time = detect_compile_time(text)
    expected = COMPILE_TIMES[version]
    if compile_time and expected not in compile_time:
        warn(f"COMPILE_TIME 为「{compile_time}」，教程记录的是 {version} →「{expected}」")
        warn("payload 目标地址必须与固件版本对应；不一致时继续有风险，请自行判断")
        if not args.yes and ask("确认仍按此版本继续？输入版本号确认，直接回车退出: ") != version:
            err("已取消，未发送任何请求")
            hint_restore()
            return 1

    step(7, 8, "发送触发请求")
    trigger = load_trigger(version)
    if trigger is None:
        hint_restore()
        return 1
    name, address = TRIGGERS[version]
    ok(f"匹配触发文件：{name}（目标 0x{address:08X}）")
    print()
    print(f"  即将向 http://{DEVICE_HOST}:{DEVICE_PORT}/OTA 发送 {len(trigger)} B，只发一次。")
    print("  发送后 TC1 会离开配网热点、转连 PHICOMM_OTA_AP 并下载 mico_ota.bin；")
    print("  连接中断 / 超时 / 无线掉线都是预期现象，不要重复发送。")
    print("  前置条件：2.4 GHz 开放热点 PHICOMM_OTA_AP（无密码、不隐藏、无客户端隔离）。")
    if not args.yes and not confirm("输入 YES 发送: "):
        info("已取消，未发送")
        hint_restore()
        return 0

    try:
        status, payload = http_call("POST", "/OTA", body=trigger, timeout=30)
        body_text = decode_bytes(payload).strip()
        ok(f"HTTP {status}：{body_text or '(空响应)'}")
        if not body_text.upper().startswith("OTA"):
            warn("响应不是预期的 OTA AAAA…，先观察设备状态，不要立刻重复发送")
    except Exception as exc:
        warn(f"请求中断：{exc.__class__.__name__}: {exc}")
        info("设备可能已切走或无线已断开，这属于预期现象，继续等待 TFTP 传输")

    step(8, 8, "等待设备下载并重启")
    info(f"监听 UDP {TFTP_HOST}:{TFTP_PORT}，最长等 {args.timeout} s；不要断电、不要拔网线")
    success = wait_for_transfer(server, args.timeout)

    print()
    hr("=")
    if success:
        ok("固件已完整下发，设备正在校验 MD5、切换应用槽并重启")
        print("""
  接下来：
  1. 等 TC1 重启，出现开放热点 TC1_A1-AP-XXXXXX
  2. 电脑或手机连该热点，浏览器打开 http://192.168.0.1/
  3. 先配好日常 Wi-Fi，再到固件更新页升级最新兼容稳定版（签名 OTA + SHA256SUMS）
  4. 升级重启后核对版本、六路名称与配置""")
    else:
        if server.failed:
            err(f"传输未完成：{server.failed}")
        else:
            err(f"等待 {args.timeout} s 仍没有完成的传输")
        print("""  排查：
  - 热点必须是 2.4 GHz、SSID 精确为 PHICOMM_OTA_AP、开放无密码、关闭客户端隔离
  - 设备是否真的进了工厂 OTA 模式（配网热点应当消失）
  - 防火墙是否放行 UDP 69 和传输会话端口
  - 镜像 MD5 是否匹配、是否超出 692224 B 应用槽""")
    hr("=")

    server.stop()
    server.join(2)

    print()
    if ask("现在恢复网络（删 10.0.0.2/8、恢复 DHCP、清理防火墙规则）？"
           "输入 y 恢复，直接回车跳过: ").lower() == "y":
        do_restore(nic)
    else:
        info("稍后可运行：python tc1_flash.py restore")
    return 0 if success else 1


def cmd_restore(args):
    if os.name != "nt":
        err("本命令依赖 Windows netsh。")
        return 2
    require_admin()
    banner()

    nic = args.nic or read_nic_name() or "以太网"
    if not args.nic:
        show_interfaces()
        nic = ask(f"要恢复的有线网卡名 [默认 {nic}]: ", nic)
    if not interface_exists(nic):
        err(f"找不到网卡「{nic}」")
        return 1
    if not args.yes and not confirm(f"确认恢复「{nic}」的网络设置？输入 YES: "):
        info("已取消")
        return 0
    do_restore(nic)
    return 0


def cmd_tftp(args):
    banner()
    firmware = load_firmware()
    if firmware is None:
        return 1
    if not is_admin():
        warn("未以管理员运行：若 10.0.0.2 还没配到网卡，绑定会失败")

    server = TftpServer(TFTP_HOST, firmware)
    server.start()
    if not wait_ready(server):
        err(server.failed or "TFTP 服务未能在 6 s 内就绪")
        return 1
    info("按 Ctrl+C 停止服务")
    try:
        while server.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        info("收到 Ctrl+C，停止服务")
    server.stop()
    server.join(2)
    if server.failed and not server.client:
        err(server.failed)
        return 1
    return 0


# ---------------------------------------------------------------- 入口
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="TC1 A1 免拆刷机向导（Windows）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", default="flash",
                        choices=["flash", "restore", "tftp"],
                        help="flash=刷机向导（默认）, restore=恢复网络, tftp=只跑 TFTP")
    parser.add_argument("--nic", help="有线网卡名称")
    parser.add_argument("--timeout", type=int, default=900, help="等待下载的超时秒数（默认 900）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（危险）")
    parser.add_argument("--no-pause", action="store_true", help="结束时不等待回车")
    args = parser.parse_args(argv)

    setup_console()
    handlers = {"flash": cmd_flash, "restore": cmd_restore, "tftp": cmd_tftp}
    try:
        code = handlers[args.command](args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print()
        warn("已中断。若网络已被修改，请运行：python tc1_flash.py restore")
        code = 130

    if not args.no_pause and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\n按回车退出 …")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
