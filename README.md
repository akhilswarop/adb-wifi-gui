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
2. **Connect**. The app tries, in order: the port that worked last time, the phone's mDNS announcement,
   then a scan of ports 30000–50000 on each address. Tick **Scan all ports** for a full 1–65535 scan.
3. If the phone says it doesn't trust this PC (adb shows `offline`), use **Pair...**: on the phone open
   *Wireless debugging > Pair device with pairing code* and type the port and code shown there.
   The code is sent to adb on stdin, never put on a command line, never logged and never saved.
4. **Install APK...**, **Launch app** and **Screenshot** (saved to `Pictures\adb-wifi`).
5. **Reset adb** restarts the adb server if it gets stuck.

A quiet check every 8 seconds keeps the status line current.

## What is stored

Only phone names, addresses, the app package and the APK path, in `%APPDATA%\adb-wifi\phones.json`.
No ports, pairing codes or keys.

## Tests

```bash
python -m unittest discover -s tests
```

The tests use a fake adb, so no phone is needed.
