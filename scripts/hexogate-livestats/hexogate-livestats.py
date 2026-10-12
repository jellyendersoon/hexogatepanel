#!/usr/bin/env python3
"""hexogate-livestats: reports this server's live traffic to the panel's "Live fleet" view.

Every INTERVAL seconds: network speed of the default interface, established TCP connections, UDP sockets, CPU
(busy and steal), memory and load. Every PORTS_EVERY seconds also the established connections per listening port,
which the panel maps to inbounds. Reads only /proc and `ss`; changes nothing on the server.

Config (/etc/hexogate/livestats.conf, KEY=VALUE):
  URL=https://panel.example/api/fleet/live/ingest   TOKEN=...   SERVER=<the node address as the panel knows it>
  optional: CONNECT_IP=<dial this IP for URL's host>  IFACE=eth0  INTERVAL=5  PORTS_EVERY=30
"""
import http.client
import json
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

CONF = sys.argv[1] if len(sys.argv) > 1 else "/etc/hexogate/livestats.conf"


def load_conf():
    conf = {}
    for line in open(CONF):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            conf[key.strip()] = value.strip().strip("\"'")
    return conf


def default_iface():
    for line in open("/proc/net/route").read().splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return fields[0]
    return "eth0"


def net_bytes(iface):
    for line in open("/proc/net/dev"):
        if line.strip().startswith(iface + ":"):
            fields = line.split(":", 1)[1].split()
            return int(fields[0]), int(fields[8])
    return 0, 0


def cpu_ticks():
    fields = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    idle = fields[3] + fields[4]
    steal = fields[7] if len(fields) > 7 else 0
    return sum(fields[:8]), idle, steal


def tcp_established():
    lines = [line.split() for line in open("/proc/net/snmp") if line.startswith("Tcp:")]
    return int(dict(zip(lines[0], lines[1])).get("CurrEstab", 0)) if len(lines) == 2 else 0


def udp_sockets():
    total = 0
    for path, name in (("/proc/net/sockstat", "UDP:"), ("/proc/net/sockstat6", "UDP6:")):
        try:
            for line in open(path):
                if line.startswith(name):
                    total += int(line.split()[2])
        except OSError:
            pass
    return total


def memory():
    info = {}
    for line in open("/proc/meminfo"):
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    return total - info.get("MemAvailable", 0), total


def port_table():
    """Established connections per local LISTENING port (outbound connections are not counted)."""
    listening = set()
    out = subprocess.run(["ss", "-Hltn"], capture_output=True, text=True, timeout=20).stdout
    for line in out.splitlines():
        fields = line.split()
        if len(fields) > 3:
            listening.add(fields[3].rsplit(":", 1)[-1])
    counts = {}
    proc = subprocess.Popen(["ss", "-Htn", "state", "established"], stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        fields = line.split()
        if len(fields) > 2:
            port = fields[2].rsplit(":", 1)[-1]
            if port in listening:
                counts[port] = counts.get(port, 0) + 1
    proc.wait(timeout=20)
    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:40]
    return {port: count for port, count in top if count > 0}


class Sender:
    def __init__(self, conf):
        url = urlsplit(conf["URL"])
        self.host, self.path, self.port = url.hostname, url.path, url.port or 443
        self.connect_ip = conf.get("CONNECT_IP") or self.host
        self.token = conf["TOKEN"]
        self.conn = None

    def _connect(self):
        ctx = ssl.create_default_context()
        raw = socket.create_connection((self.connect_ip, self.port), timeout=4)
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=4, context=ctx)
        conn.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        return conn

    def send(self, sample):
        body = json.dumps(sample, separators=(",", ":"))
        headers = {"Content-Type": "application/json", "X-Fleet-Token": self.token, "Host": self.host}
        for attempt in (1, 2):  # a kept-alive connection may have been closed by the other side: reconnect once
            try:
                if self.conn is None:
                    self.conn = self._connect()
                self.conn.request("POST", self.path, body=body, headers=headers)
                response = self.conn.getresponse()
                response.read()
                return response.status
            except Exception:
                try:
                    if self.conn:
                        self.conn.close()
                finally:
                    self.conn = None
                if attempt == 2:
                    return 0
        return 0


def main():
    conf = load_conf()
    iface = conf.get("IFACE") or default_iface()
    interval = max(2.0, float(conf.get("INTERVAL", 5)))
    ports_every = max(10.0, float(conf.get("PORTS_EVERY", 30)))
    sender = Sender(conf)
    cores = sum(1 for line in open("/proc/stat") if line.startswith("cpu") and line[3].isdigit())
    rx0, tx0 = net_bytes(iface)
    total0, idle0, steal0 = cpu_ticks()
    t0 = time.monotonic()
    next_ports = 0.0
    failures = 0
    while True:
        time.sleep(interval)
        t1 = time.monotonic()
        rx1, tx1 = net_bytes(iface)
        total1, idle1, steal1 = cpu_ticks()
        dt = max(t1 - t0, 0.001)
        dticks = max(total1 - total0, 1)
        used, total_mem = memory()
        sample = {
            "server": conf["SERVER"],
            "rx_bps": max(0, rx1 - rx0) * 8 / dt,
            "tx_bps": max(0, tx1 - tx0) * 8 / dt,
            "tcp": tcp_established(),
            "udp": udp_sockets(),
            "cpu": 100.0 * (dticks - (idle1 - idle0)) / dticks,
            "steal": 100.0 * (steal1 - steal0) / dticks,
            "cores": cores,
            "mem_used": used,
            "mem_total": total_mem,
            "load1": float(open("/proc/loadavg").read().split()[0]),
        }
        if t1 >= next_ports:
            try:
                sample["ports"] = port_table()
            except Exception:
                pass
            next_ports = t1 + ports_every
        status = sender.send(sample)
        failures = 0 if status == 204 else failures + 1
        if failures in (1, 12) or (failures and failures % 120 == 0):
            print(f"report not accepted (status {status}, {failures} in a row)", flush=True)
        rx0, tx0, total0, idle0, steal0, t0 = rx1, tx1, total1, idle1, steal1, t1


if __name__ == "__main__":
    main()
