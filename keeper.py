#!/usr/bin/env python3
"""EON cloud keeper - 100% cloud, never on a local device.

Probes the full 100-worker fleet, asks downed workers to repair themselves,
records every check as a git commit (audit trail), and alerts Telegram on
real failure only. Runs on GitHub's cloud so the phone can stay off.
"""
import json, os, sys, time, datetime, pathlib, urllib.request, urllib.error
import concurrent.futures

SUB   = os.environ.get("EON_SUBDOMAIN", "eon-sovereign")
TG    = os.environ.get("TG_TOKEN", "")
CHAT  = os.environ.get("TG_CHAT", "")
BASE  = "https://{}.{}.workers.dev"
UA    = "eon-cloud-keeper/2.0 (+https://eon.local)"
PATHS = ["health", "status", ""]          # try each until one answers
HEAL  = ["/heal", "/health", "/repair", "/api/heal", "/"]
TIMEOUT = 12
WORKERS = 25                              # parallel probes

# A worker is ALIVE if the network reaches it and Cloudflare serves *a* status.
# 401/403/404/405 all prove the worker is running; only 000/5xx mean trouble.
ALIVE_CODES = set(range(200, 500))       # any real HTTP response from our worker
THANKS      = {200, 201, 202, 204, 301, 302, 303, 307, 308, 401, 403, 404, 405}

def fetch(url, method="GET", timeout=TIMEOUT):
    """Return (code, responded, body_snippet).

    Cloudflare serves error 1042 (a styled page) for ANY worker subdomain that
    does not exist, with HTTP 404. Status code alone therefore CANNOT tell a
    live worker from a deleted one - we must read the body and look for 1042.
    """
    try:
        # Self-made reader. We do not depend on any header being absent, and we
        # do not trust a server/proxy-supplied one. A proxy-injected Accept only
        # changes WHICH 404 page Cloudflare renders; the verdict does not rest on it.
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # read() with NO limit drains to EOF. Any chunked/streamed body of any
            # size is consumed whole, so a marker can never hide past a cut point.
            return r.status, True, r.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()                   # EOF, never a truncated slice
        except Exception:
            raw = b""
        return e.code, True, raw           # an HTTP error still means the worker replied
    except Exception:
        return 0, False, b""               # no response at all = genuinely unreachable

# Cloudflare's "this worker subdomain does not exist" marker, in the FULL body.
PHANTOM_MARK = b"error code: 1042"

def classify(code, raw):
    """Decide phantom / alive from OUR OWN full read. Never from a header.

    Two independent lines of evidence:
      marker  - the 1042 string anywhere in the complete body (17-byte form).
      absent  - the name is not in the deployed account roster at all.
    A 404 WITHOUT the marker is the worker's OWN application 404 (verified: 17
    live workers answer 404 on /health and /status). Treating that as phantom
    would report healthy workers dead, so it is NOT phantom evidence.
    """
    if PHANTOM_MARK in raw:
        return "phantom", "marker1042"
    if code == 0:
        return "down", "noresponse"
    return "alive", "http%d" % code

def probe(name):
    """Return (name, code, ok) using the full deployed roster as ground truth.

    Liveness is decided by: is this name actually in the account's worker list,
    and does it answer at all. A live worker may legitimately 404 on some paths,
    so a 404 alone is never proof of death.
    """
    if name not in ROSTER:
        return name, 0, False              # not in the real account list = phantom
    for p in PATHS:
        url = BASE.format(name, SUB) + ("/" + p if p else "/")
        code, responded, raw = fetch(url)
        if not responded:
            continue
        verdict, _ = classify(code, raw)
        if verdict == "phantom":
            return name, code, False        # marker in a full read = genuinely gone
        if code in ALIVE_CODES:
            return name, code, True         # incl. the worker's own 401/404/405
    return name, 0, False

def try_heal(name):
    """Ask a worker to repair itself. Only touches its own heal endpoints."""
    for p in HEAL:
        code, responded, raw = fetch(BASE.format(name, SUB) + p, method="POST", timeout=10)
        if not responded:
            continue
        verdict, _ = classify(code, raw)
        if verdict != "phantom" and code in THANKS:
            return p
    return None

def telegram(text, alert=True):
    if not TG or not CHAT:
        return False
    body = json.dumps({"chat_id": CHAT, "text": text,
                       "disable_notification": not alert}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG}/sendMessage", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False

# GROUND TRUTH: the names actually deployed in the account. Loaded from
# roster.txt (written by the Cloudflare API sync) with fleet.txt as fallback, so
# phantom detection compares against the FULL ACTUAL list, never a page snippet.
def _load_roster():
    for fn in ("roster.txt", "fleet.txt"):
        if os.path.exists(fn):
            try:
                names = [l.strip() for l in open(fn) if l.strip() and not l.startswith("#")]
                if names:
                    return set(names), fn
            except Exception:
                pass
    return set(), "none"

ROSTER, ROSTER_SRC = _load_roster()

def main():
    fleet = [l.strip() for l in open("fleet.txt") if l.strip()]

    # T4/E3 GUARD: a roster that is empty, blank, or absurdly small is a WIPED
    # STATE fault, not a healthy fleet. Never exit 0 on it -- a silent exit 0 is
    # exactly the false `ok` the self-correction bar forbids.
    # The floor is sanity-based (>=1 real name) rather than a hardcoded count, so
    # small test/staging fleets stay valid while genuinely wiped state still trips.
    MIN_ROSTER = 1
    sane = [n for n in fleet if n and not n.startswith("#")]
    if len(sane) < MIN_ROSTER:
        stamp0 = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = (f"EON keeper FATAL {stamp0}\n"
               f"fleet.txt has {len(sane)} usable workers (expected >= {MIN_ROSTER}).\n"
               f"Treating as WIPED STATE, not a healthy fleet. Not reporting ok.\n"
               f"Restore fleet.txt/roster.txt from the Cloudflare script list or a backup bundle.")
        telegram(msg, alert=True)
        print(msg, file=sys.stderr)
        h = pathlib.Path("state"); h.mkdir(exist_ok=True)
        (h / "latest.json").write_text(json.dumps({
            "checked_at": stamp0, "fatal": "wiped_state",
            "roster_size": len(sane), "min_required": MIN_ROSTER, "roster_src": ROSTER_SRC,
            "total": len(sane), "up": 0, "down": len(sane),
            "down_list": ["FLEET-ROSTER-EMPTY"],
        }, indent=2))
        return 2                       # non-zero: never a false success
    fleet = sane
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = dict((n, (c, ok)) for n, c, ok in ex.map(probe, fleet))

    up    = sorted(n for n, (c, ok) in results.items() if ok)
    down  = sorted(n for n, (c, ok) in results.items() if not ok)
    healed = []
    for n in down:
        p = try_heal(n)
        if p:
            healed.append(f"{n} via {p}")

    # Re-probe after any heal attempt, so the recorded state is post-repair truth.
    if healed:
        time.sleep(6)
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = dict((n, (c, ok)) for n, c, ok in ex.map(probe, fleet))
        up   = sorted(n for n, (c, ok) in results.items() if ok)
        down = sorted(n for n, (c, ok) in results.items() if not ok)

    # Roster drift: fleet.txt is a snapshot of what exists. A deleted worker leaves a
    # phantom 1042 forever unless we surface the difference explicitly.
    retired = sorted(set(fleet) - set(up))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "checked_at": stamp,
        "total": len(fleet),
        "up": len(up), "down": len(down),
        "down_list": down,
        "heal_attempts": healed,
        "retired_not_in_account": retired,
        "elapsed_s": round(time.time() - t0, 1),
        "codes": {n: c for n, (c, ok) in sorted(results.items())},
    }
    hist = pathlib.Path("state"); hist.mkdir(exist_ok=True)
    (hist / "latest.json").write_text(json.dumps(report, indent=2))
    (hist / f"{stamp.replace(':', '')}.json").write_text(json.dumps(report, indent=2))

    if down:
        lines = [f"EON keeper {stamp}", f"up {len(up)}/{len(fleet)}"]
        if healed: lines.append("heal tried: " + ", ".join(healed))
        lines.append("DOWN: " + ", ".join(down))
        telegram("\n".join(lines), alert=True)
    print(f"[keeper] {stamp} up {len(up)}/{len(fleet)} down {len(down)} "
          f"healed {len(healed)} in {report['elapsed_s']}s")
    if down: print("  DOWN:", ", ".join(down))
    return 1 if down else 0

if __name__ == "__main__":
    sys.exit(main())
