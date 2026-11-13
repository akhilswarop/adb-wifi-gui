# ADB Wi-Fi

A small Windows window for Android wireless debugging. It finds the phone's ever-changing
wireless-debugging port for you, pairs, connects, installs APKs, launches an app and takes screenshots.

Plain Python with tkinter: no packages to install.

## Requirements

- Windows with Python 3.8 or newer (tkinter is included with the python.org installer).
- Android platform-tools (`adb.exe`). The app looks in `C:\adb\platform-tools`, the Android SDK
  (`%LOCALAPPDATA%\Android\Sdk\platform-tools`) and `PATH`.
- A phone with **Developer options > Wireless debugging** turned on, on the same network as the PC
  (or on the same Tailscale network).

## Run

Double-click `ADB Wi-Fi.pyw`, or:

```bash
python "ADB Wi-Fi.pyw"
```

## Use

1. **Add** a phone: a name and one or more addresses (for example its home Wi-Fi IP and its Tailscale IP).
   Optionally an app package and an APK path.
2. **Pair with QR...** (the first time, or when the phone no longer trusts this PC), the way Android Studio
   does it: on the phone open *Wireless debugging > Pair device with QR code* and scan the code in the window.
   The app finds the phone, pairs and connects. Each code is new, works once and is never saved.
   **Pair with code...** is the manual way: type the IP, port and 6-digit code from
   *Pair device with pairing code*. Codes go to adb on stdin, never on a command line, in the log or on disk.
3. **Connect**. The app tries, in order: the port that worked last time, the phone's mDNS announcement
   (including at an address you haven't saved, which is then remembered), then a scan of ports 30000–50000
   on each address. Tick **Scan all ports** for a full 1–65535 scan.
4. **Install APK...**, **Launch app** and **Screenshot** (saved to `Pictures\adb-wifi`).
5. **Reset adb** restarts the adb server if it gets stuck.

A quiet check every 8 seconds keeps the status line current.

### Why the app does its own mDNS look-up

adb's built-in discovery listens for multicast replies. Windows blocks those when the Wi-Fi is marked
*Public*, and they can arrive on the wrong adapter when Hyper-V or VPN adapters are present. So QR pairing
can hang on the phone's "Pairing device" screen. The app asks every adapter itself, with the
"reply to me directly" bit set, and Windows lets those direct replies through. No firewall changes are needed.

The QR code is drawn by a small built-in encoder (`adbwifi/qr.py`), so there is still nothing to install.

## What is stored

Only phone names, addresses, the app package and the APK path, in `%APPDATA%\adb-wifi\phones.json`.
No ports, pairing codes or keys.

## Tests

```bash
python -m unittest discover -s tests
```

The tests use a fake adb, so no phone is needed.
