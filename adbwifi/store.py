"""Saved phones: a name and the addresses to try (home Wi-Fi, other networks, Tailscale).

Only names and IP addresses are kept, in %APPDATA%\\adb-wifi\\phones.json. Ports are not kept (they change
every time Wireless debugging is switched on) and pairing codes are never kept anywhere.
"""
import json
import os
from dataclasses import asdict, dataclass, field


def default_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "adb-wifi", "phones.json")


@dataclass
class Phone:
    name: str
    addresses: list = field(default_factory=list)       # tried in this order
    package: str = ""                                     # app to launch / default for "Launch app"
    apk: str = ""                                         # default APK for "Install APK"


def load(path=None):
    path = path or default_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return [Phone(r.get("name", "Phone"), [a for a in r.get("addresses", []) if a], r.get("package", ""), r.get("apk", ""))
                for r in raw.get("phones", [])]
    except (OSError, ValueError):
        return []


def save(phones, path=None):
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"phones": [asdict(p) for p in phones]}, f, indent=2)
    os.replace(tmp, path)
