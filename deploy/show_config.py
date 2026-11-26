#!/usr/bin/env python3
"""Print the HealthSync app setup link + QR code (scan with the iPhone camera to configure the app).

    python3 deploy/show_config.py                 # prefers the Tailscale address (works away from home)
    python3 deploy/show_config.py --lan           # use the Wi-Fi/LAN address instead
    python3 deploy/show_config.py --server http://my-mac.local:8787
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

HOME = Path(os.environ.get("APPLE_HEALTH_HOME", Path.home() / ".local/apple-health"))
PORT = int(os.environ.get("APPLE_HEALTH_PORT", "8787"))


def detect_server(lan: bool) -> str:
    candidates = []
    ts = shutil.which("tailscale")
    if ts and not lan:
        out = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True).stdout.split()
        candidates += out[:1]
    for iface in ("en0", "en1"):
        ip = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True).stdout.strip()
        if ip:
            candidates.append(ip)
    if not candidates:
        sys.exit("could not detect an address; pass --server")
    return f"http://{candidates[0]}:{PORT}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server")
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    token = (HOME / "secrets/api_token").read_text().strip()
    server = args.server or detect_server(args.lan)
    link = "healthsync://configure?" + urlencode({"server": server, "token": token})

    try:
        import qrcode  # type: ignore
    except ImportError:
        uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
        os.execv(uv, [uv, "run", "--quiet", "--no-project", "--with", "qrcode[pil]", "python", __file__, *sys.argv[1:]])

    print(f"서버 주소 : {server}")
    print(f"설정 링크 : {link}\n")
    qr = qrcode.QRCode(border=2)
    qr.add_data(link)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    png = HOME / "config-qr.png"
    qr.make_image().save(png)
    os.chmod(png, 0o600)
    print(f"\nQR 이미지: {png}  (iPhone 카메라로 스캔 → HealthSync 앱이 자동 설정됩니다)")
    if not args.no_open:
        subprocess.run(["open", str(png)])


if __name__ == "__main__":
    main()
