"""绕过本地代理（Shadowrocket/Surge/Clash 等）的 DNS 劫持

macOS 上代理工具的 TUN 模式会劫持系统 DNS，将域名解析到
198.18.0.0/15 的虚拟 IP。但代理规则可能没有正确路由所有
国内 API 流量，导致连接虚拟 IP 失败。

此模块 patch socket.getaddrinfo，当系统 DNS 返回虚假 IP
（198.18.x.x / 198.19.x.x）时，自动通过 dig @8.8.8.8 获取
真实 IP。对所有基于 Python socket 的 HTTP 库透明生效
（requests / httpx / urllib3 / akshare 等）。

安装: from system.utils.dns_bypass import install; install()
卸载: from system.utils.dns_bypass import uninstall; uninstall()
"""

import re
import socket
import subprocess
import time

from system.config.settings import DNS_CACHE_TTL

_orig_getaddrinfo = socket.getaddrinfo

# 合法域名字符
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_FAKE_IP_RANGES = [
    # Shadowrocket / Surge / Clash TUN 模式常用的虚拟 IP 段
    # 198.18.0.0/15 → 198.18.0.0 - 198.19.255.255
    (198, 18),
    (198, 19),
]

_dns_cache: dict[str, tuple[str, float]] = {}
_installed = False


def _is_fake_ip(ip: str) -> bool:
    """IP 是否在代理工具的虚拟 IP 范围内"""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
        for fa, fb in _FAKE_IP_RANGES:
            if a == fa and b == fb:
                return True
    except ValueError:
        pass
    return False


def _resolve_real_ip(hostname: str) -> str | None:
    """通过直连 DNS 获取真实 IP（绕过系统 DNS 劫持）"""
    # 域名格式校验
    if not _HOSTNAME_RE.match(hostname):
        return None
    now = time.time()
    cached = _dns_cache.get(hostname)
    if cached:
        ip, ts = cached
        if now - ts < DNS_CACHE_TTL:
            return ip
        del _dns_cache[hostname]
    for dns_server in ("@8.8.8.8", "@114.114.114.114"):
        try:
            result = subprocess.run(
                ["dig", "+short", hostname, dns_server],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", line):
                    _dns_cache[hostname] = (line, now)
                    return line
        except Exception:
            continue
    return None


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """当系统 DNS 返回虚假 IP 或直接失败时，自动用 dig 获取真实 IP"""
    # IP 地址直接透传
    if not host or _is_ip_address(host):
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    try:
        results = _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        results = []

    # 检查是否所有 IPv4 地址都是虚假 IP
    real_results = []
    fake_count = 0
    for r in results:
        af, socktype, pro, canon, sa = r
        if af == socket.AF_INET and _is_fake_ip(sa[0]):
            fake_count += 1
        else:
            real_results.append(r)

    # 有真实 IP 就行
    if real_results:
        return real_results

    # 全是假的 / 系统 DNS 直接挂了 → 用 dig 解析
    if fake_count > 0 or not results:
        real_ip = _resolve_real_ip(host)
        if real_ip:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (real_ip, port)),
                (socket.AF_INET, socket.SOCK_DGRAM, 17, "", (real_ip, port)),
            ]

    return results


def _is_ip_address(host: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\.\d+\.\d+$", host))


def install():
    """安装 DNS 绕过补丁（幂等，全局生效）"""
    global _installed
    if _installed:
        return
    socket.getaddrinfo = _patched_getaddrinfo
    _installed = True


def uninstall():
    """卸载补丁，恢复系统默认 DNS"""
    global _installed
    socket.getaddrinfo = _orig_getaddrinfo
    _installed = False
