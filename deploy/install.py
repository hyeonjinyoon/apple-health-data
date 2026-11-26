#!/usr/bin/env python3
"""Install / update the Apple Health stack on this Mac (idempotent — re-run to redeploy).

    python3 deploy/install.py            # real data only
    python3 deploy/install.py --demo     # also create health_demo and load 60 days of demo data

What it does
  1. ~/.local/apple-health/{server,secrets,logs,grafana}   (outside ~/Documents: launchd has no TCC access there)
  2. secrets: API token + ClickHouse writer/reader passwords (generated once, chmod 600)
  3. ClickHouse: users health_writer (SELECT, INSERT) / health_reader (SELECT), localhost only; migrations
  4. ingest server: uv venv + LaunchAgent local.apple-health.ingest (KeepAlive) on :8787
  5. Grafana: datasource + dashboards provisioned into the existing Grafana, restart only if changed

Defaults match this machine; override with env vars (see CONFIG below). Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = Path.home()

CONFIG = {
    "home": Path(os.environ.get("APPLE_HEALTH_HOME", HOME / ".local/apple-health")),
    "port": int(os.environ.get("APPLE_HEALTH_PORT", "8787")),
    "host": os.environ.get("APPLE_HEALTH_BIND", "0.0.0.0"),
    "ch_url": os.environ.get("CLICKHOUSE_URL", "http://127.0.0.1:8123"),
    "ch_admin_user": os.environ.get("CLICKHOUSE_ADMIN_USER", "default"),
    "ch_admin_password_file": Path(os.environ.get("CLICKHOUSE_ADMIN_PASSWORD_FILE", HOME / ".local/clickhouse/.password")),
    "ch_native_port": int(os.environ.get("CLICKHOUSE_NATIVE_PORT", "9000")),
    "grafana_provisioning": Path(os.environ.get("GRAFANA_PROVISIONING_DIR", HOME / ".local/grafana/provisioning")),
    "grafana_url": os.environ.get("GRAFANA_URL", "http://127.0.0.1:8180"),
    "grafana_restart": os.environ.get("GRAFANA_RESTART_CMD", "brew services restart grafana"),
    "uv": os.environ.get("UV", shutil.which("uv") or str(HOME / ".local/bin/uv")),
}
LABEL = "local.apple-health.ingest"
DATASOURCE_UID = "apple-health-ch"
DATABASES = ["health"]


def step(msg: str) -> None:
    print(f"\n\033[1;34m==>\033[0m {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, **kw)


def write_if_changed(path: Path, content: str, mode: int = 0o644) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        os.chmod(path, mode)
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    tmp.replace(path)
    return True


def secret(path: Path, nbytes: int = 24, urlsafe: bool = False) -> str:
    if path.exists() and path.read_text().strip():
        os.chmod(path, 0o600)
        return path.read_text().strip()
    value = secrets.token_urlsafe(nbytes) if urlsafe else secrets.token_hex(nbytes)
    write_if_changed(path, value + "\n", 0o600)
    return value


def ch_exec(sql: str, password: str) -> str:
    req = urllib.request.Request(CONFIG["ch_url"] + "/", data=sql.encode(), method="POST")
    req.add_header("X-ClickHouse-User", CONFIG["ch_admin_user"])
    req.add_header("X-ClickHouse-Key", password)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit(f"ClickHouse error for `{sql[:80]}...`: {e.read().decode()[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"ClickHouse not reachable at {CONFIG['ch_url']}: {e}")


def wait_http(url: str, timeout: float = 30, headers: dict | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    return False


def local_ips() -> dict[str, str]:
    ips: dict[str, str] = {}
    for iface in ("en0", "en1"):
        out = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True).stdout.strip()
        if out:
            ips[f"LAN ({iface})"] = out
    ts = shutil.which("tailscale")
    if ts:
        out = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True).stdout.strip().splitlines()
        if out:
            ips["Tailscale"] = out[0]
    return ips


# ----------------------------------------------------------------------------- steps


def setup_dirs(h: Path) -> None:
    step(f"디렉터리 준비: {h}")
    for d in ("secrets", "logs", "grafana/dashboards"):
        (h / d).mkdir(parents=True, exist_ok=True)
    os.chmod(h / "secrets", 0o700)


def setup_clickhouse(h: Path, admin_pw: str) -> tuple[str, str]:
    step("ClickHouse 사용자/권한 설정")
    writer_pw = secret(h / "secrets/ch_writer_password")
    reader_pw = secret(h / "secrets/ch_reader_password")
    for user, pw, privs in (("health_writer", writer_pw, "SELECT, INSERT"), ("health_reader", reader_pw, "SELECT")):
        ch_exec(f"CREATE USER IF NOT EXISTS {user} IDENTIFIED WITH sha256_password BY '{pw}' HOST LOCAL", admin_pw)
        ch_exec(f"ALTER USER {user} IDENTIFIED WITH sha256_password BY '{pw}' HOST LOCAL", admin_pw)
        for db in ("health", "health_demo"):
            ch_exec(f"GRANT {privs} ON {db}.* TO {user}", admin_pw)
        info(f"{user}: {privs} on health.*, health_demo.* (localhost only)")
    return writer_pw, reader_pw


def deploy_server(h: Path, admin_pw_file: Path, databases: list[str]) -> None:
    step("Ingest 서버 배포 (uv)")
    dst = h / "server"
    src = REPO / "server"
    ignore = shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", "tests", "*.pyc")
    if dst.exists():
        for child in dst.iterdir():  # remove stale code, keep the venv
            if child.name == ".venv":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
    run([CONFIG["uv"], "sync", "--frozen", "--no-dev", "--quiet"], cwd=dst)
    py = str(dst / ".venv/bin/python")
    for db in databases:
        out = run([py, "-m", "app.migrate", "--database", db, "--url", CONFIG["ch_url"],
                   "--user", CONFIG["ch_admin_user"], "--password-file", str(admin_pw_file)],
                  cwd=dst, capture_output=True).stdout.strip()
        info(out)


def write_env(h: Path, token: str, writer_pw: str, database: str, port: int, name: str = "env") -> Path:
    env = (
        f"HEALTH_API_TOKEN={token}\n"
        f"HEALTH_CH_URL={CONFIG['ch_url']}\n"
        "HEALTH_CH_USER=health_writer\n"
        f"HEALTH_CH_PASSWORD={writer_pw}\n"
        f"HEALTH_CH_DATABASE={database}\n"
        f"HEALTH_BIND={CONFIG['host']}\n"
        f"HEALTH_PORT={port}\n"
    )
    path = h / name
    write_if_changed(path, env, 0o600)
    return path


def install_launch_agent(h: Path) -> None:
    step(f"LaunchAgent 등록: {LABEL}")
    run_sh = h / "run.sh"
    write_if_changed(run_sh, f"""#!/bin/bash
# Started by launchd ({LABEL}). Usage: run.sh [envfile]
set -euo pipefail
ENV_FILE="${{1:-{h}/env}}"
set -a; source "$ENV_FILE"; set +a
cd "{h}/server"
exec .venv/bin/python -m uvicorn app.main:app --host "$HEALTH_BIND" --port "$HEALTH_PORT" \\
     --proxy-headers --no-server-header --log-level info
""", 0o755)
    plist = HOME / f"Library/LaunchAgents/{LABEL}.plist"
    write_if_changed(plist, f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>{run_sh}</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>WorkingDirectory</key><string>{h}</string>
    <key>StandardOutPath</key><string>{h}/logs/ingest.log</string>
    <key>StandardErrorPath</key><string>{h}/logs/ingest.log</string>
</dict>
</plist>
""")
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    time.sleep(1)
    run(["launchctl", "bootstrap", domain, str(plist)])
    url = f"http://127.0.0.1:{CONFIG['port']}/health"
    if not wait_http(url):
        sys.exit(f"ingest server did not come up — check {h}/logs/ingest.log")
    info(f"running → {url}")


def setup_grafana(h: Path, tz: str | None) -> None:
    step("Grafana 대시보드/데이터소스 프로비저닝")
    dash_dir = h / "grafana/dashboards"
    cmd = [sys.executable, str(REPO / "grafana/build_dashboards.py"), "--out", str(dash_dir),
           "--datasource-uid", DATASOURCE_UID]
    if tz:
        cmd += ["--tz", tz]
    info(run(cmd, capture_output=True).stdout.strip().replace("\n", "\n    "))

    prov = CONFIG["grafana_provisioning"]
    ds_yaml = f"""# Managed by apple-health-data/deploy/install.py
apiVersion: 1
datasources:
  - name: ClickHouse (Apple Health)
    type: grafana-clickhouse-datasource
    uid: {DATASOURCE_UID}
    access: proxy
    isDefault: false
    editable: false
    jsonData:
      host: 127.0.0.1
      port: {CONFIG['ch_native_port']}
      protocol: native
      secure: false
      username: health_reader
      defaultDatabase: health
    secureJsonData:
      password: $__file{{{h}/secrets/ch_reader_password}}
"""
    dash_yaml = f"""# Managed by apple-health-data/deploy/install.py
apiVersion: 1
providers:
  - name: apple-health
    folder: Apple Health
    type: file
    disableDeletion: false
    allowUiUpdates: false
    updateIntervalSeconds: 30
    options:
      path: {dash_dir}
      foldersFromFilesStructure: false
"""
    changed = write_if_changed(prov / "datasources/apple-health.yaml", ds_yaml, 0o600)
    changed |= write_if_changed(prov / "dashboards/apple-health.yaml", dash_yaml)
    if changed:
        info(f"provisioning changed → {CONFIG['grafana_restart']}")
        run(CONFIG["grafana_restart"].split(), capture_output=True)
        if not wait_http(CONFIG["grafana_url"] + "/api/health", timeout=60):
            sys.exit("Grafana did not come back up")
    else:
        info("provisioning unchanged (dashboards JSON reloads automatically within 30s)")


def load_demo(h: Path, token: str, writer_pw: str) -> None:
    step("데모 데이터 적재 (health_demo)")
    demo_port = CONFIG["port"] + 1
    env_file = write_env(h, token, writer_pw, "health_demo", demo_port, name="env.demo")
    proc = subprocess.Popen(["/bin/bash", str(h / "run.sh"), str(env_file)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_http(f"http://127.0.0.1:{demo_port}/health"):
            sys.exit("temporary demo ingest server did not start")
        run([sys.executable, str(REPO / "tools/demo_data.py"), "--url", f"http://127.0.0.1:{demo_port}",
             "--token-file", str(h / "secrets/api_token")])
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        env_file.unlink(missing_ok=True)


def firewall_hint() -> None:
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    state = subprocess.run([fw, "--getglobalstate"], capture_output=True, text=True).stdout
    if "enabled" not in state.lower():
        return
    py = os.path.realpath(CONFIG["home"] / "server/.venv/bin/python")
    apps = subprocess.run([fw, "--listapps"], capture_output=True, text=True).stdout
    if py in apps:
        step("macOS 방화벽: Python 수신 연결이 이미 허용되어 있습니다")
        return
    step("macOS 방화벽이 켜져 있습니다")
    info("iPhone에서 접속하려면 아래 명령으로 Python의 수신 연결을 한 번 허용해 주세요:")
    info(f"  sudo {fw} --add {py}")
    info(f"  sudo {fw} --unblockapp {py}")


def summary(h: Path, token: str) -> None:
    step("완료")
    port = CONFIG["port"]
    for name, ip in local_ips().items():
        info(f"서버 주소 [{name}]: http://{ip}:{port}")
    try:
        info(f"서버 주소 [Bonjour]: http://{socket.gethostname().split('.')[0]}.local:{port}")
    except OSError:
        pass
    info(f"API 토큰: {h}/secrets/api_token")
    info("앱 설정 QR/링크: python3 deploy/show_config.py")
    info(f"Grafana: {CONFIG['grafana_url']}/d/apple-health-overview")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="also create health_demo and load demo data")
    ap.add_argument("--tz", help="IANA timezone for day boundaries (default: system timezone)")
    args = ap.parse_args()

    h: Path = CONFIG["home"]
    pw_file: Path = CONFIG["ch_admin_password_file"]
    if not pw_file.exists():
        sys.exit(f"ClickHouse admin password file not found: {pw_file} (set CLICKHOUSE_ADMIN_PASSWORD_FILE)")
    admin_pw = pw_file.read_text().strip()
    if not Path(CONFIG["uv"]).exists():
        sys.exit("uv not found — install with `brew install uv`")

    setup_dirs(h)
    token = secret(h / "secrets/api_token", 32, urlsafe=True)
    databases = DATABASES + (["health_demo"] if args.demo else [])
    writer_pw, _ = setup_clickhouse(h, admin_pw)
    deploy_server(h, pw_file, databases)
    write_env(h, token, writer_pw, "health", CONFIG["port"])
    install_launch_agent(h)
    if args.demo:
        load_demo(h, token, writer_pw)
    setup_grafana(h, args.tz)
    firewall_hint()
    summary(h, token)


if __name__ == "__main__":
    main()
