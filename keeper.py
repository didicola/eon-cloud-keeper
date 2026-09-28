#!/usr/bin/env python3
"""EON cloud keeper - runs on GitHub's cloud, never on a local device.
Probes the fleet, attempts recovery through the workers' own heal endpoints,
records state as a git commit, and reports to Telegram. Fully autonomous."""
import json,os,sys,time,urllib.request,urllib.error,datetime,pathlib

SUB = os.environ.get("EON_SUBDOMAIN", "eon-sovereign")
TG  = os.environ.get("TG_TOKEN", "")
CHAT= os.environ.get("TG_CHAT", "")
BASE= f"https://{{}}.{SUB}.workers.dev"

SERVICES = [
    ("eon-p2p-cloud",  "/status",          True),
    ("eon-mcp",        "/health",          True),
    ("eon-fleet-hub",  "/",                True),
    ("eon-hub",        "/",                True),
    ("eon-fleet-syncer","/",               True),
    ("eon-neural-web", "/",                True),
    ("eon-cloud-sentinel","/",             True),
    ("eon-watchdog-1", "/",                True),
    ("eon-birth-engine","/",               True),
    ("eon-auto-deployer","/",              True),
    ("eon-docker-mcp", "/",                True),
    ("eon-multi-publisher","/",            True),
    ("eon-leviathan-agent","/",            True),
    ("eon-neural-eu",  "/",                True),
    ("eon-auto-learner","/",               True),
]
HEAL_PATHS = ["/heal", "/health", "/repair", "/api/heal"]

UA = "eon-cloud-keeper/1.0 (+https://eon.local)"

def fetch(url, timeout=12, method="GET"):
    try:
        # Cloudflare returns 403 to default Python-urllib agent; must send a real UA.
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": 200 <= r.status < 400, "status": r.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code}
    except Exception as e:
        return {"ok": False, "status": 0, "err": type(e).__name__}

def probe():
    out = {}
    for name, path, _ in SERVICES:
        out[name] = fetch(BASE.format(name) + path)
    return out

def try_heal(name):
    """Ask a worker to repair itself. Safe: only touches its own heal endpoints."""
    for p in HEAL_PATHS:
        r = fetch(BASE.format(name) + p, timeout=10, method="POST")
        if r["ok"]:
            return p
    return None

def telegram(text):
    if not TG or not CHAT:
        return False
    body = json.dumps({"chat_id": CHAT, "text": text, "disable_notification": True}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG}/sendMessage", data=body,
            headers={"content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False

def main():
    results = probe()
    up = [n for n, v in results.items() if v["ok"]]
    down = [n for n, v in results.items() if not v["ok"]]
    healed, still_down = [], []
    for n in down:
        p = try_heal(n)
        (healed if p else still_down).append(n if not p else f"{n} via {p}")
    if healed:
        time.sleep(6)
        results = probe()
        still_down = [n for n, v in results.items() if not v["ok"]]
        up = [n for n, v in results.items() if v["ok"]]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {"checked_at": stamp, "up": sorted(up), "down": sorted(still_down),
              "heal_attempts": healed, "total": len(SERVICES)}
    hist = pathlib.Path("state"); hist.mkdir(exist_ok=True)
    (hist / "latest.json").write_text(json.dumps(report, indent=2))
    (hist / f"{stamp.replace(':','')}.json").write_text(json.dumps(report, indent=2))
    if down:
        lines = [f"EON keeper {stamp}",
                 f"up {len(up)}/{len(SERVICES)}"]
        if healed: lines.append("heal tried: " + ", ".join(healed))
        if still_down: lines.append("STILL DOWN: " + ", ".join(still_down))
        telegram("\n".join(lines))
    print(json.dumps(report, indent=2))
    return 1 if still_down else 0

if __name__ == "__main__":
    sys.exit(main())
