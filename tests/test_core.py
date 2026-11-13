"""Offline tests for adbwifi.core and adbwifi.store, with a fake adb. Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from adbwifi import core, store  # noqa: E402


class FakeAdb(core.Adb):
    """Plays a phone: `ports` maps port -> what is there ('adb', 'untrusted', 'unauthorized' or 'other')."""

    def __init__(self, ports, mdns=""):
        self.ports, self.mdns, self.calls, self.connected = ports, mdns, [], {}
        super().__init__("adb", runner=self.fake, browse=lambda: [])

    def fake(self, args, timeout, stdin_text=None):
        self.calls.append((args, stdin_text))
        cmd = args[1:]
        if cmd[0] == "connect":
            host, port = cmd[1].rsplit(":", 1)
            kind = self.ports.get(int(port), "none")
            if kind == "adb":
                self.connected[cmd[1]] = "device"
                return 0, "connected to %s\n" % cmd[1], ""
            if kind in ("untrusted", "unauthorized"):
                self.connected[cmd[1]] = "offline" if kind == "untrusted" else "unauthorized"
                return 1, "failed to connect to %s\n" % cmd[1], ""
            return 1, "cannot connect to %s: refused\n" % cmd[1], ""
        if cmd[0] == "devices":
            lines = ["List of devices attached"] + ["%s\t%s product:x model:SM_S942B" % (s, st) for s, st in self.connected.items()]
            return 0, "\n".join(lines) + "\n", ""
        if cmd[0] == "disconnect":
            self.connected.pop(cmd[1], None)
            return 0, "disconnected %s\n" % cmd[1], ""
        if cmd[0] == "mdns":
            return 0, self.mdns, ""
        if cmd[0] == "pair":
            return 0, "Successfully paired to %s [guid=adb-XYZ]\n" % cmd[1], ""
        return 0, "", ""


def fake_scan(open_ports):
    return lambda host, ports, stop=None: [p for p in open_ports if p in ports]


class ParsingTests(unittest.TestCase):
    def test_devices(self):
        text = ("List of devices attached\n"
                "192.168.50.15:40899     device product:pa3q model:SM_S942B device:pa3q transport_id:3\n"
                "192.168.50.15:49152     offline\n"
                "R5CX123   unauthorized usb:1-1 transport_id:4\n")
        d = core.parse_devices(text)
        self.assertEqual([x.state for x in d], ["device", "offline", "unauthorized"])
        self.assertEqual(d[0].host, "192.168.50.15")
        self.assertEqual(d[0].model, "SM S942B")
        self.assertIsNone(d[2].host)

    def test_mdns(self):
        text = ("List of discovered mdns services\n"
                "adb-R5CX-abc\t_adb-tls-connect._tcp\t192.168.50.15:40899\n"
                "adb-R5CX-abc\t_adb-tls-pairing._tcp\t192.168.50.15:37011\n")
        self.assertEqual(core.parse_mdns(text), [("_adb-tls-connect._tcp", "192.168.50.15:40899"),
                                                ("_adb-tls-pairing._tcp", "192.168.50.15:37011")])

    def test_subnet_warning_catches_a_typo(self):
        pc = ["192.168.50.18", "100.88.30.40"]
        self.assertIsNone(core.subnet_warning("192.168.50.15", pc))
        self.assertIsNone(core.subnet_warning("100.101.10.20", pc))            # Tailscale reaches anywhere
        self.assertIn("not on this PC's networks", core.subnet_warning("192.168.7.5", pc))
        self.assertIn("not an IP address", core.subnet_warning("192.168.50.", pc))
        self.assertIn("not on Tailscale", core.subnet_warning("100.101.10.20", ["192.168.50.18"]))


class ConnectTests(unittest.TestCase):
    def test_finds_the_adb_port_among_other_open_ports(self):
        adb = FakeAdb({33329: "other", 40899: "adb", 49152: "other"})
        r = core.find_and_connect(adb, ["192.168.50.15"], log=lambda m: None, scan=fake_scan([33329, 40899, 49152]))
        self.assertTrue(r.ok)
        self.assertEqual(r.serial, "192.168.50.15:40899")

    def test_offline_means_pair_again(self):
        adb = FakeAdb({45077: "untrusted"})
        r = core.find_and_connect(adb, ["192.168.50.15"], log=lambda m: None, scan=fake_scan([45077]))
        self.assertFalse(r.ok)
        self.assertTrue(r.needs_pairing)
        self.assertNotIn("192.168.50.15:45077", adb.connected, "the offline entry is cleaned up")

    def test_unauthorized_asks_to_allow(self):
        adb = FakeAdb({41000: "unauthorized"})
        r = core.find_and_connect(adb, ["192.168.50.15"], log=lambda m: None, scan=fake_scan([41000]))
        self.assertTrue(r.unauthorized)

    def test_mdns_skips_the_scan(self):
        adb = FakeAdb({40899: "adb"}, mdns="x\t_adb-tls-connect._tcp\t192.168.50.15:40899\n")
        scanned = []
        r = core.find_and_connect(adb, ["192.168.50.15"], log=lambda m: None,
                                  scan=lambda h, p, stop=None: scanned.append(h) or [])
        self.assertTrue(r.ok)
        self.assertEqual(scanned, [])

    def test_last_port_is_tried_first(self):
        adb = FakeAdb({40899: "adb"})
        r = core.find_and_connect(adb, ["192.168.50.15"], last_ports={"192.168.50.15": 40899}, log=lambda m: None,
                                  scan=lambda h, p, stop=None: self.fail("no scan needed"))
        self.assertTrue(r.ok)

    def test_second_address_is_tried(self):
        adb = FakeAdb({40899: "adb"})
        scans = {"192.168.50.23": [], "100.101.10.20": [40899]}
        r = core.find_and_connect(adb, ["192.168.50.23", "100.101.10.20"], log=lambda m: None,
                                  scan=lambda h, p, stop=None: scans[h])
        self.assertEqual(r.serial, "100.101.10.20:40899")

    def test_nothing_found(self):
        adb = FakeAdb({})
        r = core.find_and_connect(adb, ["192.168.50.15"], log=lambda m: None, scan=fake_scan([]))
        self.assertFalse(r.ok)
        self.assertIn("Wireless debugging", r.message)

    def test_cancel_stops_before_scanning(self):
        adb = FakeAdb({})
        stop = threading.Event()
        stop.set()
        r = core.find_and_connect(adb, ["192.168.50.15"], stop=stop, log=lambda m: None, scan=lambda *a, **k: self.fail("scanned"))
        self.assertEqual(r.message, "cancelled")


class PairingTests(unittest.TestCase):
    def test_code_goes_on_stdin_and_not_on_the_command_line(self):
        adb = FakeAdb({})
        logged = []
        ok, text = adb.pair("192.168.50.15", 37011, "123456")
        logged.append(text)
        self.assertTrue(ok)
        args, stdin_text = adb.calls[-1]
        self.assertNotIn("123456", " ".join(args))
        self.assertEqual(stdin_text, "123456\n")
        self.assertNotIn("123456", " ".join(logged))


class ScanTests(unittest.TestCase):
    def test_scan_finds_a_listening_port(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertEqual(core.scan_ports("127.0.0.1", range(port - 5, port + 5), timeout=0.2, workers=10), [port])
        finally:
            srv.close()


class StoreTests(unittest.TestCase):
    def test_round_trip_keeps_no_ports_or_codes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "phones.json")
            store.save([store.Phone("Test phone", ["192.168.50.15", "100.101.10.20"], "com.example.app", "C:/x.apk")], path)
            back = store.load(path)
            self.assertEqual(back[0].addresses, ["192.168.50.15", "100.101.10.20"])
            raw = open(path, encoding="utf-8").read()
            self.assertNotIn("port", raw.lower())
            self.assertNotIn("code", raw.lower())

    def test_missing_file_is_empty(self):
        self.assertEqual(store.load(os.path.join(tempfile.gettempdir(), "nope", "phones.json")), [])


class QrPairingTests(unittest.TestCase):
    MDNS = ("List of discovered mdns services\n"
            "adbwifi-AbCd2345\t_adb-tls-pairing._tcp\t192.168.50.15:37011\n"
            "adb-R5CX-abc\t_adb-tls-connect._tcp\t192.168.50.15:40899\n")

    def test_payload_is_the_android_studio_format(self):
        name, password, text = core.new_qr_pairing()
        self.assertEqual(text, "WIFI:T:ADB;S:%s;P:%s;;" % (name, password))
        self.assertTrue(name.startswith("adbwifi-"))
        self.assertGreaterEqual(len(password), 12)
        self.assertNotEqual(core.new_qr_pairing()[1], password, "a fresh password every time")
        for ch in ";:,\\\" ":
            self.assertNotIn(ch, name + password)

    def test_parse_named(self):
        self.assertEqual(core.parse_mdns_named(self.MDNS)[0],
                         ("adbwifi-AbCd2345", "_adb-tls-pairing._tcp", "192.168.50.15:37011"))

    def test_waits_for_our_name_only(self):
        adb = FakeAdb({}, mdns="other-name\t_adb-tls-pairing._tcp\t192.168.50.99:30000\n")
        ticks = []

        def sleep(_):
            ticks.append(1)
            if len(ticks) == 2:                      # the phone scans on the third poll
                adb.mdns += self.MDNS
        self.assertEqual(core.wait_for_pairing_service(adb, "adbwifi-AbCd2345", sleep=sleep), "192.168.50.15:37011")
        self.assertEqual(len(ticks), 2)

    def test_wait_stops_on_cancel(self):
        stop = threading.Event()
        stop.set()
        self.assertIsNone(core.wait_for_pairing_service(FakeAdb({}), "x", stop=stop))

    def test_password_goes_on_stdin_and_guid_is_read(self):
        adb = FakeAdb({})
        ok, out = adb.pair("192.168.50.15", 37011, "Xk7mQ2pL9vRt")
        self.assertEqual(adb.calls[-1][1], "Xk7mQ2pL9vRt\n")
        self.assertNotIn("Xk7mQ2pL9vRt", " ".join(adb.calls[-1][0]))
        self.assertEqual(core.pair_guid(out), "adb-XYZ")

    def test_connects_to_the_announced_port_after_pairing(self):
        adb = FakeAdb({40899: "adb"}, mdns=self.MDNS)
        r = core.connect_after_pairing(adb, "192.168.50.15", "adb-R5CX-abc", log=lambda m: None, sleep=lambda s: None)
        self.assertTrue(r.ok)
        self.assertEqual(r.serial, "192.168.50.15:40899")


def _mdns_reply(instance, service, port, ip):
    """A phone's mDNS answer: PTR -> SRV -> A, with name compression like Android sends."""
    import socket
    import struct

    def name(n):
        return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"
    svc = name(service)
    ptr_target = bytes([len(instance)]) + instance.encode() + b"\xc0\x0c"      # instance + pointer to service
    host = name("Android_TEST.local")
    srv = struct.pack(">HHH", 0, 0, port) + host
    pkt = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 2)
    pkt += svc + struct.pack(">HHIH", 12, 1, 120, len(ptr_target)) + ptr_target
    inst_off = 12 + len(svc) + 10
    pkt += b"\xc0" + bytes([inst_off]) + struct.pack(">HHIH", 33, 0x8001, 120, len(srv)) + srv
    pkt += host + struct.pack(">HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(ip)
    return pkt


class MdnsTests(unittest.TestCase):
    def test_parses_a_phone_reply(self):
        data = _mdns_reply("adb-SERIAL1-abc123", "_adb-tls-connect._tcp.local", 33171, "192.168.50.8")
        self.assertEqual(core.parse_mdns_reply(data, "192.168.50.8"),
                         [("adb-SERIAL1-abc123", "_adb-tls-connect._tcp", "192.168.50.8:33171")])

    def test_garbage_is_ignored(self):
        self.assertEqual(core.parse_mdns_reply(b"\x00\x01\x02", "1.2.3.4"), [])

    def test_query_asks_for_both_services_with_unicast_replies(self):
        pkt = core.mdns_query_packet()
        self.assertIn(b"_adb-tls-pairing", pkt)
        self.assertIn(b"_adb-tls-connect", pkt)
        self.assertEqual(pkt.count(b"\x00\x0c\x80\x01"), 2)

    def test_connect_finds_the_phone_at_a_new_address(self):
        adb = FakeAdb({33171: "adb"})
        adb.browse = lambda: [("adb-SERIAL1-abc", "_adb-tls-connect._tcp", "192.168.50.8:33171")]
        r = core.find_and_connect(adb, ["192.168.50.15"], log=lambda m: None,
                                  scan=lambda h, p, stop=None: self.fail("no scan needed"))
        self.assertTrue(r.ok)
        self.assertEqual(r.serial, "192.168.50.8:33171")

    def test_qr_wait_uses_our_own_lookup(self):
        adb = FakeAdb({})
        adb.browse = lambda: [("adbwifi-Zz", "_adb-tls-pairing._tcp", "192.168.50.8:41234")]
        self.assertEqual(core.wait_for_pairing_service(adb, "adbwifi-Zz", sleep=lambda s: None), "192.168.50.8:41234")


class QrEncoderTests(unittest.TestCase):
    def test_sizes(self):
        from adbwifi import qr
        self.assertEqual(len(qr.encode("A")), 21)
        self.assertEqual(len(qr.encode(core.new_qr_pairing()[2])), 33)      # version 4
        with self.assertRaises(ValueError):
            qr.encode("x" * 300)

    def test_a_camera_can_read_it(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV not installed")
        from adbwifi import qr
        det = cv2.QRCodeDetector()
        for text in ("WIFI:T:ADB;S:adbwifi-AbCd2345;P:Xk7mQ2pL9vRt;;", "y" * 100, "z" * 200):
            for mask in (None, 0, 3, 7):
                m = qr.encode(text, mask)
                n = len(m) + 8
                img = np.full((n, n), 255, np.uint8)
                for r, row in enumerate(m):
                    for c, dark in enumerate(row):
                        if dark:
                            img[r + 4, c + 4] = 0
                img = cv2.resize(img, (n * 8, n * 8), interpolation=cv2.INTER_NEAREST)
                self.assertEqual(det.detectAndDecode(img)[0], text, "mask %s" % mask)


if __name__ == "__main__":
    unittest.main()
