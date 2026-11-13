"""Everything the window does, without the window: finding adb, listing devices, scanning for the
wireless-debugging port, connecting, pairing, installing and taking screenshots.

Rules this module keeps:
- One adb binary for everything (two different adb versions kill each other's server).
- A pairing code is passed to adb on stdin, never on the command line (which other programs can read)
  and never written to the log or to disk.
- Ports are not saved anywhere: the phone picks a new one each time Wireless debugging is switched on.
"""
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# Where adb is looked for, in order. The first one that exists is used.
ADB_CANDIDATES = [
    r"C:\adb\platform-tools\adb.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
]
SCAN_PORTS = range(30000, 50001)          # where Android has put the port in practice
WIDE_PORTS = range(1024, 65536)           # the fallback when the normal range finds nothing
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------------------------------------------------ finding adb

def find_adbs():
    """Every distinct adb binary on this PC: the known install places plus whatever PATH gives."""
    found = []
    for path in ADB_CANDIDATES + [shutil.which("adb") or ""]:
        if path and os.path.isfile(path):
            real = os.path.normcase(os.path.realpath(path))
            if real not in [os.path.normcase(os.path.realpath(p)) for p in found]:
                found.append(path)
    return found


def adb_version(path, runner=None):
    rc, out, _ = (runner or run_process)([path, "version"], 10)
    m = re.search(r"Version\s+(\S+)", out)
    return m.group(1) if m else "unknown"


def run_process(args, timeout, stdin_text=None):
    """Runs a program without a console window and returns (exit code, stdout, stderr)."""
    try:
        p = subprocess.run(args, input=stdin_text, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timed out after %ss" % timeout
    except OSError as e:
        return -1, "", str(e)


# ------------------------------------------------------------------ parsing adb output

@dataclass
class Device:
    serial: str
    state: str                     # device, offline, unauthorized, ...
    props: dict = field(default_factory=dict)

    @property
    def host(self):
        return self.serial.rsplit(":", 1)[0] if re.match(r"^[\d.]+:\d+$", self.serial) else None

    @property
    def model(self):
        return self.props.get("model", "").replace("_", " ")


def parse_devices(text):
    """Parses `adb devices -l` output."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        out.append(Device(parts[0], parts[1], props))
    return out


def parse_mdns(text):
    """Parses `adb mdns services`: returns (service type, 'ip:port') for each wireless-debugging service."""
    found = []
    for line in text.splitlines():
        m = re.search(r"(_adb-tls-(?:connect|pairing)\._tcp)\S*\s+(\d+\.\d+\.\d+\.\d+:\d+)", line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def parse_mdns_named(text):
    """Like parse_mdns, with the service's instance name first: (name, service type, 'ip:port')."""
    found = []
    for line in text.splitlines():
        m = re.match(r"\s*(\S+)\s+(_adb-tls-(?:connect|pairing)\._tcp)\S*\s+(\d+\.\d+\.\d+\.\d+:\d+)", line)
        if m:
            found.append((m.group(1), m.group(2), m.group(3)))
    return found


# ------------------------------------------------------------------ QR pairing (as Android Studio does it)
# The PC shows a QR code holding a made-up service name and password. The phone scans it, then announces
# a pairing service under that name over mDNS; the PC finds it and runs `adb pair` with the password.

_QR_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_qr_pairing():
    """A fresh (service name, password, QR text). Used once; nothing is saved."""
    import secrets
    name = "adbwifi-" + "".join(secrets.choice(_QR_ALPHABET) for _ in range(8))
    password = "".join(secrets.choice(_QR_ALPHABET) for _ in range(12))
    return name, password, "WIFI:T:ADB;S:%s;P:%s;;" % (name, password)


def wait_for_pairing_service(adb, name, stop=None, timeout=180, every=1.0, sleep=None):
    """Polls adb's mDNS list until the phone announces the pairing service `name`. Returns 'ip:port',
    or None on timeout or cancel."""
    import time
    sleep = sleep or time.sleep
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return None
        for n, kind, addr in adb.discover():
            if n == name and kind.startswith("_adb-tls-pairing"):
                return addr
        sleep(every)
    return None


def pair_guid(text):
    """The phone's id from adb's 'Successfully paired to ... [guid=adb-XXXX-yyyy]' line, or ''."""
    m = re.search(r"guid=([^\]\s]+)", text)
    return m.group(1) if m else ""


# ------------------------------------------------------------------ the network

def local_ipv4s():
    """This PC's IPv4 addresses (LAN, Tailscale, ...), from the hostname plus a UDP-socket trick."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    for probe in ("192.168.1.1", "100.100.100.100", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 9))
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


# ------------------------------------------------------------------ our own mDNS look-up
# adb's built-in discovery listens for multicast replies, which Windows blocks on a "Public" Wi-Fi and
# which can go to the wrong network adapter. Asking with the "unicast reply" bit set makes the phone answer
# straight to our socket, which Windows lets through (a reply to our own question) on every adapter.

MDNS_ADDR = ("224.0.0.251", 5353)
ADB_SERVICES = ("_adb-tls-pairing._tcp.local", "_adb-tls-connect._tcp.local")


def _dns_name(data, off):
    labels, end = [], None
    for _ in range(128):
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:                         # a pointer to a name earlier in the packet
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | data[off + 1]
            continue
        labels.append(data[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if end is not None else off)


def mdns_query_packet(services=ADB_SERVICES):
    import struct
    body = b""
    for svc in services:
        for part in svc.split("."):
            body += bytes([len(part)]) + part.encode()
        body += b"\0" + struct.pack(">HH", 12, 0x8001)   # PTR, class IN with the unicast-reply bit
    return struct.pack(">HHHHHH", 0, 0, len(services), 0, 0, 0) + body


def parse_mdns_reply(data, src_ip=None):
    """The adb services in one mDNS reply: [(instance name, service type, 'ip:port')]."""
    import struct
    try:
        qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
        off = 12
        for _ in range(qd):
            off = _dns_name(data, off)[1] + 4
        ptr, srv, addr = [], {}, {}
        for _ in range(an + ns + ar):
            name, off = _dns_name(data, off)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            if rtype == 12:
                ptr.append((name.lower(), _dns_name(data, off)[0]))
            elif rtype == 33:
                srv[name.lower()] = (_dns_name(data, off + 6)[0].lower(), struct.unpack(">H", data[off + 4:off + 6])[0])
            elif rtype == 1 and rdlen == 4:
                addr[name.lower()] = socket.inet_ntoa(data[off:off + 4])
            off += rdlen
    except (IndexError, struct.error):
        return []
    found = []
    for svc, full in ptr:
        kind = svc[:-len(".local")] if svc.endswith(".local") else svc
        if not kind.startswith("_adb-tls-"):
            continue
        target_port = srv.get(full.lower())
        if not target_port:
            continue
        ip = addr.get(target_port[0]) or src_ip
        if ip:
            found.append((full[:-len(svc) - 1] if full.lower().endswith("." + svc) else full, kind,
                          "%s:%d" % (ip, target_port[1])))
    return found


def mdns_browse(timeout=1.5, interfaces=None):
    """Asks every network adapter for adb services; returns [(instance name, service type, 'ip:port')]."""
    import select
    import time
    pkt = mdns_query_packet()
    socks = []
    for ip in interfaces if interfaces is not None else local_ipv4s():
        if ip.startswith("169.254."):
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.bind((ip, 0))
            s.sendto(pkt, MDNS_ADDR)
            socks.append(s)
        except OSError:
            continue
    found, end, resent = [], time.monotonic() + timeout, False
    try:
        while socks and time.monotonic() < end:
            if not resent and time.monotonic() > end - timeout / 2:     # one repeat, in case a packet was lost
                resent = True
                for s in socks:
                    try:
                        s.sendto(pkt, MDNS_ADDR)
                    except OSError:
                        pass
            ready = select.select(socks, [], [], 0.2)[0]
            for s in ready:
                try:
                    data, src = s.recvfrom(9000)
                except OSError:
                    continue
                for item in parse_mdns_reply(data, src[0]):
                    if item not in found:
                        found.append(item)
    finally:
        for s in socks:
            s.close()
    return found


def subnet_warning(phone_ip, pc_ips):
    """A plain-language warning when a phone address cannot be on any of this PC's networks (a typo,
    or the PC and the phone on different Wi-Fi). Tailscale addresses (100.64.0.0/10) are fine anywhere."""
    try:
        ip = ipaddress.ip_address(phone_ip)
    except ValueError:
        return "%r is not an IP address." % phone_ip
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return None if any(ipaddress.ip_address(p) in ipaddress.ip_network("100.64.0.0/10") for p in pc_ips) else \
            "This PC is not on Tailscale, so the phone's Tailscale address cannot be reached."
    for p in pc_ips:
        if ip in ipaddress.ip_network(p + "/24", strict=False):
            return None
    return "%s is not on this PC's networks (%s). Check the address on the phone, or that both are on the same Wi-Fi." % (
        phone_ip, ", ".join(pc_ips) or "none found")


def scan_ports(host, ports=SCAN_PORTS, timeout=0.4, workers=400, stop=None, progress=None):
    """Open TCP ports on `host`, in order. `stop` (a threading.Event) cancels the scan."""
    ports = list(ports)
    done = [0]
    lock = threading.Lock()

    def probe(port):
        if stop is not None and stop.is_set():
            return None
        s = socket.socket()
        s.settimeout(timeout)
        try:
            return port if s.connect_ex((host, port)) == 0 else None
        except OSError:
            return None
        finally:
            s.close()
            with lock:
                done[0] += 1
                if progress and done[0] % 2000 == 0:
                    progress(done[0], len(ports))

    with ThreadPoolExecutor(workers) as pool:
        return [p for p in pool.map(probe, ports) if p]


# ------------------------------------------------------------------ adb

class Adb:
    """A thin wrapper around one adb binary. `runner(args, timeout, stdin_text)` can be replaced in tests."""

    def __init__(self, path, runner=None, browse=None):
        self.path = path
        self.runner = runner or run_process
        self.browse = browse or mdns_browse          # our own mDNS look-up (replaced in tests)

    def run(self, *args, timeout=20, stdin_text=None):
        return self.runner([self.path, *args], timeout, stdin_text)

    def devices(self):
        return parse_devices(self.run("devices", "-l")[1])

    def device(self, serial):
        return next((d for d in self.devices() if d.serial == serial), None)

    def connect(self, host, port):
        rc, out, err = self.run("connect", "%s:%d" % (host, port), timeout=15)
        text = (out + err).strip()
        return text.startswith("connected") or text.startswith("already connected"), text

    def disconnect(self, serial=None):
        return self.run("disconnect", *([serial] if serial else []))[1].strip()

    def pair(self, host, port, code):
        """Pairs with the code on stdin (adb asks for it when it is not on the command line)."""
        rc, out, err = self.run("pair", "%s:%d" % (host, port), timeout=30, stdin_text=code.strip() + "\n")
        text = (out + err).strip()
        return "Successfully paired" in text, text.replace(code.strip(), "******")

    def kill_server(self):
        return self.run("kill-server")[1].strip()

    def mdns_services(self):
        return parse_mdns(self.run("mdns", "services", timeout=10)[1])

    def mdns_named(self):
        return parse_mdns_named(self.run("mdns", "services", timeout=10)[1])

    def discover(self):
        """Wireless-debugging services seen by our own mDNS look-up, plus whatever adb's discovery has."""
        found = []
        for source in (self.browse, self.mdns_named):
            try:
                found += [s for s in source() if s not in found]
            except Exception:
                pass
        return found

    def getprop(self, serial, name):
        return self.run("-s", serial, "shell", "getprop", name, timeout=10)[1].strip()

    def install(self, serial, apk):
        rc, out, err = self.run("-s", serial, "install", "-r", apk, timeout=300)
        text = (out + err).strip()
        return "Success" in text, text

    def launch(self, serial, package):
        rc, out, err = self.run("-s", serial, "shell", "monkey", "-p", package, "-c",
                                "android.intent.category.LAUNCHER", "1", timeout=20)
        text = (out + err).strip()
        return "Events injected: 1" in text, text

    def screenshot(self, serial, dest):
        """Saves a PNG straight from the phone (binary, so not through the text runner)."""
        try:
            with open(dest, "wb") as f:
                p = subprocess.run([self.path, "-s", serial, "exec-out", "screencap", "-p"], stdout=f,
                                   stderr=subprocess.PIPE, timeout=30, creationflags=NO_WINDOW)
            ok = p.returncode == 0 and os.path.getsize(dest) > 1000
            return ok, dest if ok else p.stderr.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)


# ------------------------------------------------------------------ finding and connecting a phone

@dataclass
class ConnectResult:
    ok: bool
    serial: str = ""
    message: str = ""
    needs_pairing: bool = False
    unauthorized: bool = False


def try_port(adb, host, port):
    """Connects to one port and says what it really is: a working connection, a phone that no longer
    trusts this PC (adb shows it as offline), or not the adb port at all."""
    serial = "%s:%d" % (host, port)
    ok, text = adb.connect(host, port)
    dev = adb.device(serial)
    if dev and dev.state == "device":
        return ConnectResult(True, serial, "connected to %s" % serial)
    if dev and dev.state == "unauthorized":
        return ConnectResult(False, serial, "the phone is asking to allow USB debugging from this PC", unauthorized=True)
    if dev and dev.state == "offline":
        adb.disconnect(serial)
        return ConnectResult(False, serial, "the phone answered on %s but no longer trusts this PC" % serial, needs_pairing=True)
    if dev:
        adb.disconnect(serial)
    return ConnectResult(False, serial, text or "no answer")


def connect_after_pairing(adb, host, guid="", stop=None, log=print, wait=10, sleep=None):
    """Right after a pairing the phone announces its connect port over mDNS (and adb may connect by
    itself). Waits for either, up to `wait` seconds. Returns a ConnectResult, or None if neither came."""
    import time
    sleep = sleep or time.sleep
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and not (stop is not None and stop.is_set()):
        for d in adb.devices():                          # adb's own auto-connect
            if d.state == "device" and (d.host == host or (guid and guid in d.serial)):
                return ConnectResult(True, d.serial, "connected to %s" % d.serial)
        for name, kind, addr in adb.discover():
            if kind.startswith("_adb-tls-connect") and (addr.startswith(host + ":") or (guid and name == guid)):
                log("Found by mDNS: %s" % addr)
                h, p = addr.rsplit(":", 1)
                r = try_port(adb, h, int(p))
                if r.ok or r.unauthorized:
                    return r
        sleep(1)
    return None


def find_and_connect(adb, hosts, last_ports=None, stop=None, log=print, scan=scan_ports, wide=False):
    """Tries, in order: the ports that worked earlier this session, adb's own mDNS discovery, then a
    port scan of each address. Stops at the first working connection."""
    hosts = [h for h in hosts if h]
    pairing_hint = None

    # 1. a port that worked earlier in this session
    for host in hosts:
        port = (last_ports or {}).get(host)
        if port:
            log("Trying %s:%d (worked earlier)..." % (host, port))
            r = try_port(adb, host, port)
            if r.ok or r.unauthorized:
                return r
            pairing_hint = pairing_hint or (r if r.needs_pairing else None)

    # 2. mDNS: the phone announces its port on the same Wi-Fi
    services = [(n, k, a) for n, k, a in adb.discover() if k.startswith("_adb-tls-connect")]
    # the saved addresses first; then any other phone on the Wi-Fi (its address may have changed). Only a
    # phone that already trusts this PC accepts the connection, so trying the others is harmless.
    services.sort(key=lambda s: s[2].rsplit(":", 1)[0] not in hosts)
    for _name, kind, addr in services:
        host, port = addr.rsplit(":", 1)
        log("Found by mDNS: %s%s" % (addr, "" if host in hosts else " (not a saved address)"))
        r = try_port(adb, host, int(port))
        if r.ok or r.unauthorized:
            return r
        if host in hosts:
            pairing_hint = pairing_hint or (r if r.needs_pairing else None)

    # 3. scan each address
    for host in hosts:
        if stop is not None and stop.is_set():
            return ConnectResult(False, message="cancelled")
        ports = WIDE_PORTS if wide else SCAN_PORTS
        log("Scanning %s, ports %d-%d..." % (host, ports[0], ports[-1]))
        open_ports = scan(host, ports, stop=stop)
        log("Open ports on %s: %s" % (host, ", ".join(map(str, open_ports)) or "none"))
        for port in open_ports:
            r = try_port(adb, host, port)
            log("  %d: %s" % (port, r.message))
            if r.ok or r.unauthorized:
                return r
            pairing_hint = pairing_hint or (r if r.needs_pairing else None)

    if pairing_hint:
        return ConnectResult(False, pairing_hint.serial, pairing_hint.message + ". Pair again.", needs_pairing=True)
    return ConnectResult(False, message="No wireless-debugging port answered. Is Wireless debugging on and the phone awake?")
