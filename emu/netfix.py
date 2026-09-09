# -*- coding: utf-8 -*-
"""
Обход DNS-подмены провайдера (RPZ) для ЧТЕНИЯ публичных API котировок.

Факт 06.09.2026: с текущей сети ноутбука DNS-резолвер (10.139.217.177) отдаёт для
api.smarkets.com, guest.api.arcadia.pinnacle.com, api.betfair.com адрес redirect.rpz.rs
(217.65.192.244) — блок-лист сербского регулятора. api.sx.bet не тронут.

Что делает модуль: если системный резолвер вернул RPZ-адрес, имя резолвится через
DNS-over-HTTPS Cloudflare (1.1.1.1, по IP — без системного DNS) и результат подставляется
в socket.getaddrinfo только для этого процесса. Системные настройки не меняются.
Включение: автоматически при импорте (можно отключить переменной NETFIX=0).
"""

import json
import os
import socket
import ssl
import time
import urllib.request

RPZ_IPS = {"217.65.192.244"}
DOH_IPS = ("1.1.1.1", "1.0.0.1")
_cache = {}          # host -> (ips, expires)
_orig_getaddrinfo = socket.getaddrinfo
_enabled = os.environ.get("NETFIX", "1") != "0"


def _doh_resolve(host):
    last = None
    for ip in DOH_IPS:
        try:
            req = urllib.request.Request(f"https://{ip}/dns-query?name={host}&type=A",
                                         headers={"Accept": "application/dns-json", "Host": "cloudflare-dns.com"})
            ctx = ssl.create_default_context()
            ctx.check_hostname = False   # подключаемся по IP, сертификат Cloudflare валиден для cloudflare-dns.com
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                d = json.loads(r.read().decode())
            ips = [a["data"] for a in d.get("Answer", []) if a.get("type") == 1]
            if ips:
                return ips
        except Exception as e:
            last = e
    raise OSError(f"DoH resolve failed for {host}: {last}")


def _is_blocked(host):
    try:
        infos = _orig_getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return any(i[4][0] in RPZ_IPS for i in infos)


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if _enabled and isinstance(host, str) and host and not host.replace(".", "").isdigit():
        now = time.time()
        c = _cache.get(host)
        if c is None or c[1] < now:
            ips = None
            if _is_blocked(host):
                try:
                    ips = _doh_resolve(host)
                    print(f"[netfix] {host}: RPZ-блок провайдера, резолв через DoH → {ips[0]}")
                except Exception as e:
                    print(f"[netfix] {host}: {e}")
            _cache[host] = (ips, now + 600)
            c = _cache[host]
        if c[0]:
            res = []
            for ip in c[0]:
                res.extend(_orig_getaddrinfo(ip, port, socket.AF_INET, type or socket.SOCK_STREAM, proto, flags))
            return res
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _getaddrinfo


if __name__ == "__main__":
    for h in ("api.smarkets.com", "guest.api.arcadia.pinnacle.com", "api.sx.bet", "api.betfair.com"):
        try:
            print(h, "->", socket.getaddrinfo(h, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0])
        except Exception as e:
            print(h, "ERR", e)
    req = urllib.request.Request("https://api.smarkets.com/v3/events/45291280/markets/", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("smarkets via netfix:", r.status)
