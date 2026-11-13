"""The ADB Wi-Fi window. All slow work (scans, adb calls) runs on a worker thread; the window only
ever changes from the Tk thread, through a queue polled with `after`."""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import core, qr, store

# A placeholder for the very first start; your own phones live only in %APPDATA%\adb-wifi\phones.json.
DEFAULT_PHONE = store.Phone(name="My phone", addresses=[])
SHOTS = os.path.join(os.path.expanduser("~"), "Pictures", "adb-wifi")
GREEN, RED, AMBER, GREY = "#1e7e34", "#b02a37", "#a86400", "#555555"


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.stop = threading.Event()
        self.serial = None                      # the connected phone, if any
        self.last_ports = {}                    # host -> port that worked, this session only
        self.phones = store.load() or [DEFAULT_PHONE]
        if not os.path.exists(store.default_path()):
            store.save(self.phones)
        adbs = core.find_adbs()
        self.adb = core.Adb(adbs[0]) if adbs else None
        self.adb_note = self._adb_note(adbs)

        root.title("ADB Wi-Fi")
        root.geometry("820x660")
        root.minsize(700, 560)
        self._build()
        self._refresh_phone()
        self._log("adb: " + self.adb_note)
        self.root.after(100, self._drain)
        self.root.after(1500, self._poll_devices)

    # ------------------------------------------------------------------ layout

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(self.root, text="Phone")
        top.pack(fill="x", **pad)
        self.phone_var = tk.StringVar()
        self.phone_box = ttk.Combobox(top, textvariable=self.phone_var, state="readonly", width=28)
        self.phone_box.grid(row=0, column=0, **pad)
        self.phone_box.bind("<<ComboboxSelected>>", lambda e: self._refresh_phone())
        ttk.Button(top, text="Add...", command=self._add_phone).grid(row=0, column=1, **pad)
        ttk.Button(top, text="Edit...", command=self._edit_phone).grid(row=0, column=2, **pad)
        ttk.Button(top, text="Remove", command=self._remove_phone).grid(row=0, column=3, **pad)
        self.addr_label = ttk.Label(top, foreground=GREY)
        self.addr_label.grid(row=1, column=0, columnspan=6, sticky="w", **pad)

        conn = ttk.LabelFrame(self.root, text="Connection")
        conn.pack(fill="x", **pad)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self._connect)
        self.connect_btn.grid(row=0, column=0, **pad)
        self.wide_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(conn, text="Scan all ports (slower)", variable=self.wide_var).grid(row=0, column=1, **pad)
        self.cancel_btn = ttk.Button(conn, text="Cancel", command=self.stop.set, state="disabled")
        self.cancel_btn.grid(row=0, column=2, **pad)
        ttk.Button(conn, text="Pair with QR...", command=self._qr_pair).grid(row=0, column=3, **pad)
        ttk.Button(conn, text="Pair with code...", command=self._pair_dialog).grid(row=0, column=4, **pad)
        ttk.Button(conn, text="Disconnect", command=self._disconnect).grid(row=0, column=5, **pad)
        ttk.Button(conn, text="Reset adb", command=self._reset).grid(row=0, column=6, **pad)
        self.status = ttk.Label(conn, text="Not connected", foreground=GREY, font=("Segoe UI", 11, "bold"))
        self.status.grid(row=1, column=0, columnspan=7, sticky="w", **pad)
        self.warn = ttk.Label(conn, text="", foreground=AMBER, wraplength=760, justify="left")
        self.warn.grid(row=2, column=0, columnspan=7, sticky="w", **pad)

        act = ttk.LabelFrame(self.root, text="Actions")
        act.pack(fill="x", **pad)
        self.install_btn = ttk.Button(act, text="Install APK...", command=self._install)
        self.install_btn.grid(row=0, column=0, **pad)
        ttk.Label(act, text="App:").grid(row=0, column=1, sticky="e")
        self.pkg_var = tk.StringVar()
        ttk.Entry(act, textvariable=self.pkg_var, width=26).grid(row=0, column=2, **pad)
        self.launch_btn = ttk.Button(act, text="Launch app", command=self._launch)
        self.launch_btn.grid(row=0, column=3, **pad)
        self.shot_btn = ttk.Button(act, text="Screenshot", command=self._screenshot)
        self.shot_btn.grid(row=0, column=4, **pad)

        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(fill="both", expand=True, **pad)
        self.logbox = ScrolledText(logf, height=12, state="disabled", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)
        self._set_connected(None)

    # ------------------------------------------------------------------ helpers

    def _adb_note(self, adbs):
        if not adbs:
            return "no adb found. Install Android platform-tools to C:\\adb\\platform-tools."
        note = "%s (version %s)" % (adbs[0], core.adb_version(adbs[0]))
        if len(adbs) > 1:
            versions = {p: core.adb_version(p) for p in adbs}
            if len(set(versions.values())) > 1:
                note += ". Warning: other adb copies with different versions exist (%s); run only this one, or they stop each other's server." % \
                        ", ".join("%s %s" % (p, v) for p, v in versions.items() if p != adbs[0])
        return note

    def phone(self):
        names = [p.name for p in self.phones]
        name = self.phone_var.get()
        return self.phones[names.index(name)] if name in names else (self.phones[0] if self.phones else None)

    def _refresh_phone(self):
        self.phone_box["values"] = [p.name for p in self.phones]
        p = self.phone()
        if p and self.phone_var.get() != p.name:
            self.phone_var.set(p.name)
        self.addr_label.config(text=("Addresses: " + ", ".join(p.addresses)) if p and p.addresses else "No addresses saved")
        self.pkg_var.set(p.package if p else "")
        warns = []
        pc = core.local_ipv4s()
        for a in (p.addresses if p else []):
            w = core.subnet_warning(a, pc)
            if w and not a.startswith("100."):
                warns.append(w)
        self.warn.config(text="")
        if warns and len(warns) == len([a for a in p.addresses if not a.startswith("100.")]):
            self.warn.config(text="None of this phone's Wi-Fi addresses is on this PC's network right now (Tailscale may still work).")

    def _log(self, text):
        self.logbox.config(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def _set_connected(self, serial, info=""):
        self.serial = serial
        if serial:
            self.status.config(text="Connected: " + (info or serial), foreground=GREEN)
        else:
            self.status.config(text="Not connected", foreground=GREY)
        for b in (self.install_btn, self.launch_btn, self.shot_btn):
            b.config(state="normal" if serial and not self.busy else "disabled")

    def _set_busy(self, on):
        self.busy = on
        self.connect_btn.config(state="disabled" if on else "normal")
        self.cancel_btn.config(state="normal" if on else "disabled")
        self._set_connected(self.serial, self.status.cget("text").replace("Connected: ", "") if self.serial else "")

    def _run(self, work, done=None):
        """Runs `work()` on a worker thread; `done(result)` then runs on the Tk thread."""
        if self.busy or not self.adb:
            if not self.adb:
                messagebox.showerror("ADB Wi-Fi", "No adb found.")
            return
        self.stop.clear()
        self._set_busy(True)

        def body():
            try:
                result = work()
            except Exception as e:      # shown in the log, never swallowed silently
                result = e
            self.q.put(("done", done, result))
        threading.Thread(target=body, daemon=True).start()

    def _post_log(self, text):
        self.q.put(("log", text))

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._log(item[1])
                elif item[0] == "call":
                    item[1]()
                elif item[0] == "poll":
                    _, serial, state = item
                    if serial == self.serial and state != "device" and not self.busy:
                        self._log("Lost the connection to %s (%s)." % (serial, state or "gone"))
                        self._set_connected(None)
                elif item[0] == "done":
                    _, done, result = item
                    self._set_busy(False)
                    if isinstance(result, Exception):
                        self._log("Error: %s" % result)
                    elif done:
                        done(result)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _poll_devices(self):
        """Every few seconds: notice when the connected phone goes away (Wi-Fi changed, debugging switched off)."""
        if self.adb and not self.busy and self.serial:
            serial = self.serial

            def check():                        # a quiet check: it never blocks the buttons
                d = self.adb.device(serial)
                self.q.put(("poll", serial, d.state if d else None))
            threading.Thread(target=check, daemon=True).start()
        self.root.after(8000, self._poll_devices)

    # ------------------------------------------------------------------ connection

    def _connect(self):
        p = self.phone()
        if not p or not p.addresses:
            messagebox.showinfo("ADB Wi-Fi", "Add the phone's address first (Edit...).")
            return
        hosts = list(p.addresses)
        wide = self.wide_var.get()
        self._log("Connecting to %s (%s)..." % (p.name, ", ".join(hosts)))

        def work():
            r = core.find_and_connect(self.adb, hosts, self.last_ports, stop=self.stop, log=self._post_log, wide=wide)
            info = ""
            if r.ok:
                model = self.adb.getprop(r.serial, "ro.product.model")
                release = self.adb.getprop(r.serial, "ro.build.version.release")
                info = "%s, Android %s, at %s" % (model or "phone", release or "?", r.serial)
            return r, info

        def done(res):
            r, info = res
            self._log(r.message)
            if r.ok:
                host, port = r.serial.rsplit(":", 1)
                self.last_ports[host] = int(port)
                self._set_connected(r.serial, info)
                self._remember_address(host)
            else:
                self._set_connected(None)
                if r.unauthorized:
                    messagebox.showinfo("ADB Wi-Fi", "Unlock the phone and tap Allow on the debugging prompt, then press Connect again.")
                elif r.needs_pairing:
                    if messagebox.askyesno("ADB Wi-Fi", "The phone answered but no longer trusts this PC.\n\nPair again now?"):
                        self._pair_dialog(r.serial.rsplit(":", 1)[0] if r.serial else None)
        self._run(work, done)

    def _remember_address(self, host):
        """Adds a newly seen address to the selected phone, so the next Connect tries it first."""
        cur = self.phone()
        if cur and host not in cur.addresses:
            cur.addresses.insert(0, host)
            store.save(self.phones)
            self._log("Added %s to %s's addresses." % (host, cur.name))
            self._refresh_phone()

    def _qr_pair(self):
        """Pairs the way Android Studio does: show a QR code, the phone scans it and announces itself over
        mDNS, then `adb pair` with the QR's one-time password, then connect."""
        if self.busy or not self.adb:
            return self._run(lambda: None)              # shows the usual 'no adb' message if needed
        name, password, text = core.new_qr_pairing()
        matrix = qr.encode(text)
        dlg = tk.Toplevel(self.root)
        dlg.title("Pair with QR code")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        ttk.Label(dlg, text="On the phone: Settings > Developer options > Wireless debugging >\n"
                            "Pair device with QR code, then scan this.",
                  justify="left").pack(padx=12, pady=(10, 4), anchor="w")
        scale, quiet = 7, 4
        side = (len(matrix) + 2 * quiet) * scale
        canvas = tk.Canvas(dlg, width=side, height=side, bg="white", highlightthickness=0)
        canvas.pack(padx=12, pady=6)
        for r, row in enumerate(matrix):
            for c, dark in enumerate(row):
                if dark:
                    x, y = (c + quiet) * scale, (r + quiet) * scale
                    canvas.create_rectangle(x, y, x + scale, y + scale, fill="black", width=0)
        state = ttk.Label(dlg, text="Waiting for the phone to scan...", foreground=GREY)
        state.pack(padx=12, pady=4, anchor="w")
        ttk.Label(dlg, text="The phone and this PC must be on the same Wi-Fi. The code works once and is never saved.",
                  foreground=GREY, wraplength=side).pack(padx=12, anchor="w")
        ttk.Button(dlg, text="Cancel", command=lambda: close()).pack(pady=10)
        p = self.phone()
        others = list(p.addresses) if p else []

        def show(msg):
            self.q.put(("call", lambda: state.winfo_exists() and state.config(text=msg)))

        def close():
            self.stop.set()
            if dlg.winfo_exists():
                dlg.destroy()
        dlg.protocol("WM_DELETE_WINDOW", close)
        self._log("Showing a pairing QR code (%s). Waiting for the phone to scan it..." % name)

        def work():
            addr = core.wait_for_pairing_service(self.adb, name, stop=self.stop)
            if not addr:
                return None, "cancelled" if self.stop.is_set() else \
                    "The phone did not show up within 3 minutes. Same Wi-Fi? Try again, or use Pair with code."
            host, port = addr.rsplit(":", 1)
            show("Found the phone at %s. Pairing..." % host)
            self._post_log("The phone scanned the code; pairing with %s..." % addr)
            ok, out = self.adb.pair(host, int(port), password)
            self._post_log(out)
            if not ok:
                return None, "Pairing failed: " + out
            show("Paired. Connecting...")
            r = core.connect_after_pairing(self.adb, host, core.pair_guid(out), stop=self.stop, log=self._post_log)
            if not r:
                hosts = [host] + [h for h in others if h != host]
                r = core.find_and_connect(self.adb, hosts, self.last_ports, stop=self.stop, log=self._post_log)
            info = ""
            if r.ok:
                info = "%s, Android %s, at %s" % (self.adb.getprop(r.serial, "ro.product.model") or "phone",
                                                  self.adb.getprop(r.serial, "ro.build.version.release") or "?", r.serial)
            return r, info

        def done(res):
            r, info = res
            if dlg.winfo_exists():
                dlg.destroy()
            if r is None:
                self._log(info)
                if info != "cancelled":
                    messagebox.showinfo("Pair with QR code", info)
                return
            self._log(r.message)
            if r.ok:
                host, port = r.serial.rsplit(":", 1)
                self.last_ports[host] = int(port)
                self._set_connected(r.serial, info)
                self._remember_address(host)
            elif r.unauthorized:
                messagebox.showinfo("ADB Wi-Fi", "Unlock the phone and tap Allow on the debugging prompt, then press Connect.")
        self._run(work, done)

    def _pair_dialog(self, host=None):
        p = self.phone()
        dlg = tk.Toplevel(self.root)
        dlg.title("Pair with the phone")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(dlg, text="On the phone: Settings > Developer options > Wireless debugging >\n"
                            "Pair device with pairing code. Type what it shows here.",
                  justify="left").grid(row=0, column=0, columnspan=2, padx=10, pady=8, sticky="w")
        host_var = tk.StringVar(value=host or (p.addresses[0] if p and p.addresses else ""))
        port_var, code_var = tk.StringVar(), tk.StringVar()
        for i, (label, var, show) in enumerate((("IP address", host_var, None), ("Pairing port", port_var, None),
                                               ("Pairing code", code_var, None)), start=1):
            ttk.Label(dlg, text=label).grid(row=i, column=0, padx=10, pady=4, sticky="e")
            e = ttk.Entry(dlg, textvariable=var, width=24, show=show or "")
            e.grid(row=i, column=1, padx=10, pady=4, sticky="w")
            if i == 2:
                e.focus_set()
        ttk.Label(dlg, text="The code is used once and never saved.", foreground=GREY).grid(
            row=4, column=0, columnspan=2, padx=10, sticky="w")

        def go():
            host_, port_, code = host_var.get().strip(), port_var.get().strip(), code_var.get().strip()
            if not (port_.isdigit() and code.isdigit() and len(code) == 6 and host_):
                messagebox.showerror("Pair", "Enter the IP address, the pairing port (digits) and the 6-digit code.", parent=dlg)
                return
            code_var.set("")                                    # the code does not stay in the window
            dlg.destroy()
            self._log("Pairing with %s:%s..." % (host_, port_))

            def work():
                return self.adb.pair(host_, int(port_), code)

            def done(res):
                ok, text = res
                self._log(text)
                if ok:
                    self._log("Paired. Connecting...")
                    self.root.after(500, self._connect)
                else:
                    messagebox.showerror("Pair", "Pairing failed:\n" + text)
            self._run(work, done)

        btns = ttk.Frame(dlg)
        btns.grid(row=5, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="Pair", command=go).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda e: go())

    def _disconnect(self):
        def work():
            return self.adb.disconnect(self.serial) if self.serial else self.adb.disconnect()
        def done(text):
            self._log(text or "disconnected")
            self._set_connected(None)
        self._run(work, done)

    def _reset(self):
        def work():
            return self.adb.kill_server()
        def done(text):
            self._log("adb server stopped (it starts again on the next command). " + (text or ""))
            self._set_connected(None)
        self._run(work, done)

    # ------------------------------------------------------------------ actions

    def _install(self):
        p = self.phone()
        start = p.apk if p and p.apk and os.path.exists(p.apk) else ""
        apk = filedialog.askopenfilename(title="APK to install", filetypes=[("Android app", "*.apk")],
                                         initialdir=os.path.dirname(start) if start else None,
                                         initialfile=os.path.basename(start) if start else None)
        if not apk:
            return
        serial = self.serial
        self._log("Installing %s..." % os.path.basename(apk))
        self._run(lambda: self.adb.install(serial, apk),
                  lambda res: self._log(("Installed. " if res[0] else "Install failed: ") + res[1].splitlines()[-1] if res[1] else ""))

    def _launch(self):
        pkg, serial = self.pkg_var.get().strip(), self.serial
        if not pkg:
            return
        self._run(lambda: self.adb.launch(serial, pkg),
                  lambda res: self._log(("Launched " + pkg) if res[0] else ("Could not launch %s: %s" % (pkg, res[1][-200:]))))

    def _screenshot(self):
        os.makedirs(SHOTS, exist_ok=True)
        dest = os.path.join(SHOTS, time.strftime("phone_%Y%m%d_%H%M%S.png"))
        serial = self.serial

        def done(res):
            ok, where = res
            self._log(("Saved " + where) if ok else ("Screenshot failed: " + where))
            if ok:
                os.startfile(where)
        self._run(lambda: self.adb.screenshot(serial, dest), done)

    # ------------------------------------------------------------------ phones

    def _phone_dialog(self, phone=None):
        dlg = tk.Toplevel(self.root)
        dlg.title("Phone")
        dlg.transient(self.root)
        dlg.grab_set()
        name = tk.StringVar(value=phone.name if phone else "")
        addrs = tk.StringVar(value=", ".join(phone.addresses) if phone else "")
        pkg = tk.StringVar(value=phone.package if phone else "")
        apk = tk.StringVar(value=phone.apk if phone else "")
        rows = (("Name", name), ("Addresses (Wi-Fi, Tailscale), comma separated", addrs),
                ("App to launch (package)", pkg), ("Default APK", apk))
        for i, (label, var) in enumerate(rows):
            ttk.Label(dlg, text=label).grid(row=i, column=0, padx=10, pady=4, sticky="e")
            ttk.Entry(dlg, textvariable=var, width=46).grid(row=i, column=1, padx=10, pady=4)
        ttk.Button(dlg, text="Browse...", command=lambda: apk.set(
            filedialog.askopenfilename(filetypes=[("Android app", "*.apk")]) or apk.get())).grid(row=3, column=2, padx=6)
        result = {}

        def ok():
            addresses = [a.strip() for a in addrs.get().replace(";", ",").split(",") if a.strip()]
            bad = [a for a in addresses if core.subnet_warning(a, []) and "is not an IP address" in (core.subnet_warning(a, []) or "")]
            if not name.get().strip() or bad:
                messagebox.showerror("Phone", "Give a name and valid IP addresses." + (" Not valid: " + ", ".join(bad) if bad else ""), parent=dlg)
                return
            result["phone"] = store.Phone(name.get().strip(), addresses, pkg.get().strip(), apk.get().strip())
            dlg.destroy()
        ttk.Button(dlg, text="Save", command=ok).grid(row=5, column=1, pady=10, sticky="e")
        self.root.wait_window(dlg)
        return result.get("phone")

    def _add_phone(self):
        p = self._phone_dialog()
        if p:
            self.phones.append(p)
            store.save(self.phones)
            self.phone_var.set(p.name)
            self._refresh_phone()

    def _edit_phone(self):
        old = self.phone()
        if not old:
            return self._add_phone()
        p = self._phone_dialog(old)
        if p:
            self.phones[self.phones.index(old)] = p
            store.save(self.phones)
            self.phone_var.set(p.name)
            self._refresh_phone()

    def _remove_phone(self):
        p = self.phone()
        if p and messagebox.askyesno("ADB Wi-Fi", "Remove %s from the list?" % p.name):
            self.phones.remove(p)
            store.save(self.phones)
            self.phone_var.set("")
            self._refresh_phone()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
