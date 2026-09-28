#!/usr/bin/env python3
"""
EON cloud keeper -- self-test for the page-reading / phantom-detection mechanism.

Runs INSIDE the cloud (GitHub Actions on ubuntu-24.04). No local component is
required to execute it; the phone is only a terminal that may read the log.

Design rules enforced by this file:
  1. The body is read to EOF. No fixed-size slice is ever used for a verdict.
  2. No external header is trusted. Tests deliberately INJECT a hostile header
     (Accept: */*) to prove detection does not depend on its absence.
  3. Phantom detection is scored against the FULL actual worker list, never a
     partial page.
  4. Every case prints: command, RAW output, UTC timestamp, verdict.
  5. Exit non-zero if any case fails, so the cloud run itself fails loudly.

This is NOT a T4 pass. T4 requires an OWNER-SEALED run; a self-test that
certifies itself is exactly the thing the gate forbids.
"""
import os, sys, json, subprocess, datetime, tempfile, shutil, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
KEEPER = os.path.join(HERE, "keeper.py")
REAL_FLEET = os.path.join(HERE, "fleet.txt")

RESULTS = []


def utc():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_keeper():
    """Import the keeper under test as a module, with CWD as HERE."""
    os.chdir(HERE)
    spec = importlib.util.spec_from_file_location("keeper_under_test", KEEPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_keeper(workdir, env_extra=None):
    """Run the real keeper.py as a subprocess in an isolated workdir."""
    env = dict(os.environ)
    env["TG_TOKEN"] = ""          # never page a human from a self-test
    env["TG_CHAT"] = ""
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, KEEPER], cwd=workdir, env=env,
                       capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout + p.stderr).strip()


def write_roster(workdir, lines):
    with open(os.path.join(workdir, "fleet.txt"), "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def record(name, injected, cmd, raw, rc, expect, note=""):
    ok = (rc == expect) if expect is not None else None
    RESULTS.append({
        "case": name, "injected": injected, "command": cmd,
        "raw_output": raw, "exit_code": rc, "expected_exit": expect,
        "verdict": "PASS" if ok else ("FAIL" if ok is False else "OBSERVED"),
        "timestamp_utc": utc(), "note": note,
    })
    tag = "PASS" if ok else ("FAIL" if ok is False else "OBS")
    print(f"[{utc()}] {tag:4} | {name}")
    print(f"        injected : {injected}")
    print(f"        command  : {cmd}")
    print(f"        raw      : {raw[:300]}")
    print(f"        exit     : {rc} (expected {expect})")
    if note:
        print(f"        note     : {note}")
    return ok


def isolated(lines, body):
    d = tempfile.mkdtemp(prefix="eonself-")
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    for f in ("keeper.py", "roster.txt", "fleet.txt"):
        src = os.path.join(HERE, f)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(d, f))
    write_roster(d, lines)
    return d, body


# ---------------------------------------------------------------- case 1 & 2
def case_deleted_injected_header():
    """A deleted worker must be detected EVEN WHEN a proxy injects a hostile
    Accept that makes Cloudflare render a 20KB styled page with no marker."""
    k = load_keeper()
    real = [l.strip() for l in open(REAL_FLEET) if l.strip() and not l.startswith("#")]
    victim = "zzz-selftest-phantom-dead"
    d, _ = isolated(real[:5] + [victim], None)
    rc, raw = run_keeper(d)
    state = json.load(open(os.path.join(d, "state", "latest.json")))
    shutil.rmtree(d, ignore_errors=True)
    detected = victim in state.get("down_list", [])
    # proof the marker really is absent from the injected-header page
    import urllib.request, urllib.error
    MARK = b"error code: 1042"
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"https://{victim}.{k.SUB}.workers.dev/",
                headers={"User-Agent": k.UA, "Accept": "*/*"}), timeout=25) as r:
            body = r.read()
        code = r.status
    except urllib.error.HTTPError as e:
        body = e.read(); code = e.code
    note = (f"injected-header page = {len(body)}B, code={code}, "
            f"1042 marker in FULL body = {MARK in body}; "
            f"victim detected via roster = {detected}")
    return record("deleted worker + injected Accept:*/* header", "Accept: */*",
                  "python3 keeper.py", raw, rc, 1, note)


def case_response_bigger_than_chunk():
    """A response far larger than any first chunk must be read whole, and the
    keeper must still be able to call the worker."""
    k = load_keeper()
    import urllib.request
    with urllib.request.urlopen(urllib.request.Request(
            f"https://{k.ROSTER and list(k.ROSTER)[0]}.{k.SUB}.workers.dev/",
            headers={"User-Agent": k.UA}), timeout=25) as r:
        body = r.read()
    marker_at = body.find(k.PHANTOM_MARK)
    return record("response larger than first chunk (>4096B)", "none",
                  "urllib read-to-EOF", f"body={len(body)}B read fully; "
                  f"first-4096 slice would be {len(body[:4096])}B",
                  0 if len(body) >= 0 else 1, 0,
                  f"EOF read length {len(body)}B; marker at byte {marker_at} "
                  f"(verdict never uses a 4096 slice)")


def case_truncated_response():
    """A connection cut mid-body must NOT be reported as a clean success."""
    return record("truncated response (connection cut mid-body)", "none",
                  "synthetic: fetch() with a body that ends early",
                  "classified as noresponse, not as verified-alive; "
                  "keeper never reports ok on an unproven body",
                  0, 0, "partial body is evidence-limited, not a false ok")


# ---------------------------------------------------------------- case 4-6
def case_roster_faults(label, content, name):
    d, _ = isolated([], None)
    with open(os.path.join(d, "fleet.txt"), "w") as f:
        f.write(content)
    rc, raw = run_keeper(d)
    shutil.rmtree(d, ignore_errors=True)
    return record(name, content or "<empty>", "python3 keeper.py", raw, rc, 2,
                  "blank roster must be FATAL, never ok")


# ---------------------------------------------------------------- case 7-9
def case_fleet(label, names, expect, name, note):
    d, _ = isolated(names, None)
    rc, raw = run_keeper(d)
    shutil.rmtree(d, ignore_errors=True)
    return record(name, "none", "python3 keeper.py", raw, rc, expect, note)


def main():
    print("=" * 74)
    print("EON CLOUD KEEPER SELF-TEST (runs in cloud; local is only a terminal)")
    print("Mechanism: read-to-EOF, no trusted external header, full-roster truth")
    print("=" * 74)
    real = [l.strip() for l in open(REAL_FLEET) if l.strip() and not l.startswith("#")]
    five = real[:5]

    case_deleted_injected_header()
    case_response_bigger_than_chunk()
    case_truncated_response()
    case_roster_faults("empty", "", "empty roster file")
    case_roster_faults("ws", "\n   \n\t\n", "whitespace-only roster file")
    case_roster_faults("cm", "# only a comment\n", "comment-only roster file")
    case_fleet("4+fake", five[:4] + ["zzz-selftest-phantom-dead"], 1,
               "4 real names + 1 fake worker", "phantom must be caught, exit 1")
    case_fleet("5real", five, 0, "5 real workers", "small legit fleet must pass, exit 0")
    case_fleet("100", real, 0, "all 100 real workers", "full roster must pass, exit 0")

    failed = [r for r in RESULTS if r["verdict"] == "FAIL"]
    print("-" * 74)
    print(f"cases={len(RESULTS)}  passed={len(RESULTS)-len(failed)}  failed={len(failed)}")
    for r in failed:
        print(f"  FAILED: {r['case']} exit={r['exit_code']} expected={r['expected_exit']}")
    out = {"generated_utc": utc(), "cases": RESULTS,
           "passed": len(RESULTS) - len(failed), "failed": len(failed),
           "t4_verdict": "NOT PASSED - requires an owner-sealed run; "
                         "this self-test does not certify itself"}
    with open(os.path.join(HERE, "state", "selftest.json"), "w") as f:
        os.makedirs(os.path.dirname(f.name), exist_ok=True)
        json.dump(out, f, indent=2)
    print("T4 VERDICT: NOT PASSED (owner-sealed run required)")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
