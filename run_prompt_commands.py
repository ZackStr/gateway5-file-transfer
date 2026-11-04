#!/usr/bin/env python3
"""Run CLI commands on a network device and answer their interactive prompts.

Gateway5 python-script service. The device connection (host, user, password,
platform) is NOT passed as an argument: gateway5 pipes the targeted inventory
node to stdin as InventoryInfo JSON, so the credentials come from Inventory
Manager and never appear in the calling workflow:

    {"inventory_nodes": [{"name": "...", "attributes": {
        "itential_host": "...", "itential_user": "...", "itential_password": "...",
        "itential_platform": "cisco_ios", "itential_port": 22}}]}

Input (--steps, a JSON array, run in order over ONE SSH session):

    [{"command": "install remove inactive",
      "prompts": [{"expect": "\\[y/n\\]", "respond": "y"}],
      "timeout": 300,
      "fail_on": ["FAILED"]}]

Each step has:
    command   CLI command to send (required).
    prompts   prompts this command may raise (optional). Each prompt:
                expect     regex matched against the END of the output the
                           device has produced since the last answer.
                respond    text to send (required). "" (or "\\n") presses
                           Enter with nothing typed.
                optional   true if the prompt may not appear (default false).
                sensitive  true to hide the response text in the result.
    timeout   seconds allowed for the step (default 120).
    fail_on   optional. Extra regexes that mark the step failed when found in
              its output (a line starting with "%" is always an error).
    expect_output
              optional. A regex, or a list of regexes that must ALL match,
              that the step's output has to contain once the device prompt
              returns; if not, the step fails. Omit it and no outcome check is
              made. Works with or without fail_on.

Behaviour:
  * Prompts are matched in any order; each is answered at most once.
  * Only a prompt listed for that step is ever answered. If the device stops
    at anything else, the step fails and nothing is sent.
  * The step is finished when the device's normal CLI prompt returns. A
    required prompt that never appeared fails the step.
  * The first failed step stops the run; later steps are reported as skipped.
  * The device prompt is recognised from the hostname learned at connect. If that
    does not match (slow devices can yield a truncated prompt), a prompt-shaped last
    line that has been idle for 1.5 s also ends the step ("prompt_matched": "generic").
    A step that still times out reports "diagnostics" (the regex tried and the raw tail).
  * Netmiko options on the inventory node (global_delay_factor, banner_timeout,
    auth_timeout, conn_timeout/timeout) are honoured; fast_cli is off when a delay
    factor above 1 is set.

Result: one JSON object on stdout. Exit code 0 only when every step succeeded.
"device_prompt" is the hostname prompt netmiko learned at connect (the value the
end-of-command check is built from), returned on every run to help diagnose prompt issues.
"""

import argparse
import json
import re
import sys
import time

from netmiko import ConnectHandler

DEFAULT_STEP_TIMEOUT = 120
POLL = 0.2
ERROR_LINE = re.compile(r"^\s*%\s*(Invalid|Unknown|Ambiguous|Incomplete|Error)", re.I | re.M)
# Fallback end-of-command check that does not depend on the hostname netmiko learned at
# connect: the output's last line is a single token ending in # or > (optionally with a
# "(config)" mode suffix). Only trusted after the output has been idle for GENERIC_IDLE
# seconds, so a pause mid-output cannot end a step early.
GENERIC_PROMPT = re.compile(r"(?:^|\n)[^\s#>]+(?:\([^)\n]*\))?[#>][ \t]*\Z")
GENERIC_IDLE = 1.5


def _read_inventory_node():
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    nodes = (data or {}).get("inventory_nodes") if isinstance(data, dict) else None
    return nodes[0] if nodes else None


def _connection(node, platform_override):
    attrs = (node or {}).get("attributes") or {}
    host, user, password = (attrs.get("itential_host"), attrs.get("itential_user"),
                            attrs.get("itential_password"))
    missing = [n for n, v in (("itential_host", host), ("itential_user", user),
                              ("itential_password", password)) if not v]
    if missing:
        raise SystemExit("inventory node is missing: " + ", ".join(missing))
    opts = (attrs.get("itential_driver_options") or {}).get("netmiko") or {}
    kwargs = dict(
        device_type=platform_override or opts.get("device_type")
        or attrs.get("itential_platform") or "cisco_ios",
        host=host, username=user, password=password,
        port=int(opts.get("port") or attrs.get("itential_port") or 22),
        conn_timeout=int(opts.get("conn_timeout") or opts.get("timeout") or 15),
        # legacy ssh-rsa host keys are disabled by default in paramiko 3.3+
        disabled_algorithms={"pubkeys": []},
        fast_cli=True,
    )
    # Honour the node's own pacing options (set for slow devices such as the 3850).
    # fast_cli shortens netmiko's read delays, so turn it off when a delay is requested.
    for key in ("banner_timeout", "auth_timeout"):
        if opts.get(key):
            kwargs[key] = int(opts[key])
    gdf = opts.get("global_delay_factor")
    if gdf and float(gdf) > 1:
        kwargs["global_delay_factor"] = float(gdf)
        kwargs["fast_cli"] = False
    return kwargs, ((node or {}).get("name") or host)


def _validate(steps):
    """Return (normalised steps, error). Compiles every regex before connecting."""
    if not isinstance(steps, list) or not steps:
        return None, "steps must be a non-empty JSON array"
    out = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not isinstance(s.get("command"), str) or not s["command"].strip():
            return None, f"step {i}: 'command' must be a non-empty string"
        prompts = []
        for j, p in enumerate(s.get("prompts") or []):
            if not isinstance(p, dict) or not isinstance(p.get("expect"), str) or not p["expect"]:
                return None, f"step {i} prompt {j}: 'expect' must be a non-empty string"
            if "respond" not in p or not isinstance(p["respond"], str):
                return None, f"step {i} prompt {j}: 'respond' is required (use \"\" to press Enter)"
            try:
                rx = re.compile(p["expect"] + r"[ \t\r\n]*\Z")
            except re.error as e:
                return None, f"step {i} prompt {j}: bad regex: {e}"
            prompts.append({"expect": p["expect"], "rx": rx, "respond": p["respond"],
                            "optional": bool(p.get("optional")), "sensitive": bool(p.get("sensitive"))})
        fail_on = []
        for f in s.get("fail_on") or []:
            try:
                fail_on.append(re.compile(f))
            except (re.error, TypeError) as e:
                return None, f"step {i}: bad fail_on regex: {e}"
        expect_output = []
        eo = s.get("expect_output")
        if eo is not None:
            eo = [eo] if isinstance(eo, str) else eo
            if not isinstance(eo, list) or not all(isinstance(x, str) and x for x in eo):
                return None, f"step {i}: 'expect_output' must be a non-empty string or a list of them"
            for x in eo:
                try:
                    expect_output.append(re.compile(x, re.M))
                except re.error as e:
                    return None, f"step {i}: bad expect_output regex: {e}"
        out.append({"command": s["command"], "prompts": prompts, "fail_on": fail_on,
                    "expect_output": expect_output,
                    "timeout": float(s.get("timeout") or DEFAULT_STEP_TIMEOUT)})
    return out, None


def _run_step(conn, step, prompt_rx):
    """Send one command and drive its prompts. Returns a result dict."""
    res = {"command": step["command"], "status": "failed", "error": None,
           "output": "", "prompts_answered": [], "elapsed": 0.0}
    start = time.monotonic()
    remaining = list(step["prompts"])
    conn.clear_buffer()
    conn.write_channel(step["command"] + "\n")
    buf, pos = "", 0
    last_change, generic = time.monotonic(), False
    while True:
        chunk = conn.read_channel()
        if chunk:
            buf += chunk.replace("\r", "")
            last_change = time.monotonic()
        seg = buf[pos:]
        matched = next((p for p in remaining if p["rx"].search(seg[-4000:])), None)
        if matched:
            remaining.remove(matched)
            conn.write_channel(matched["respond"].rstrip("\r\n") + "\n")
            sent = ("<hidden>" if matched["sensitive"]
                    else (matched["respond"].rstrip("\r\n") or "<Enter>"))
            res["prompts_answered"].append({"expect": matched["expect"], "sent": sent})
            pos = len(buf)
            continue
        if prompt_rx.search(seg[-300:]):
            break
        if (time.monotonic() - last_change >= GENERIC_IDLE
                and GENERIC_PROMPT.search(seg[-300:])):
            generic = True
            break
        if time.monotonic() - start > step["timeout"]:
            res["output"] = buf.strip()
            res["diagnostics"] = {"prompt_regex": prompt_rx.pattern, "tail": repr(buf[-160:])}
            res["error"] = (f"timed out after {step['timeout']:.0f}s without the device prompt "
                            "returning; nothing further was sent")
            res["elapsed"] = round(time.monotonic() - start, 3)
            return res
        time.sleep(POLL)
    out = (GENERIC_PROMPT if generic else prompt_rx).sub("", buf).strip()
    if generic:
        res["prompt_matched"] = "generic"
    res["output"] = out
    res["elapsed"] = round(time.monotonic() - start, 3)
    missing = [p["expect"] for p in remaining if not p["optional"]]
    bad = ERROR_LINE.search(out) or next((f.search(out) for f in step["fail_on"] if f.search(out)), None)
    if bad:
        res["error"] = "device reported an error: " + bad.group(0).strip()
    elif missing:
        res["error"] = "required prompt never appeared: " + ", ".join(missing)
    else:
        absent = [x.pattern for x in step["expect_output"] if not x.search(out)]
        if absent:
            res["error"] = "expected output not found: " + ", ".join(absent)
        else:
            res["status"] = "success"
    return res


def _close(dev, clean):
    """Close the session. After a failed step the device may still be sitting at a
    prompt, and netmiko's normal disconnect types 'exit' into it, which some prompts
    (e.g. a delete's [confirm]) accept as an answer. So after a failure drop the
    connection without sending anything."""
    try:
        if clean:
            dev.disconnect()
        else:
            dev.remote_conn.close()
            dev.remote_conn_pre.close()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Run CLI commands and answer their prompts")
    ap.add_argument("--steps", required=True, help="JSON array of steps")
    ap.add_argument("--platform", default=None, help="Override the inventory platform (netmiko device_type)")
    args = ap.parse_args()

    try:
        steps, err = _validate(json.loads(args.steps))
    except json.JSONDecodeError as e:
        steps, err = None, f"--steps is not valid JSON: {e}"
    if err:
        print(json.dumps({"success": False, "error": err, "steps": []}))
        return 2

    node = _read_inventory_node()
    kwargs, name = _connection(node, args.platform)
    t0 = time.monotonic()
    results = []
    failed = False
    try:
        dev = ConnectHandler(**kwargs)
    except Exception as e:  # authentication / timeout / reachability
        print(json.dumps({"success": False, "device": name,
                          "error": f"could not connect: {type(e).__name__}: {e}"[:500],
                          "steps": [{"command": s["command"], "status": "skipped"} for s in steps]}))
        return 1
    try:
        base = re.escape(dev.base_prompt)
        prompt_rx = re.compile(base + r"(\([^)\n]*\))?[#>][ \t]*\Z")
        for s in steps:
            if failed:
                results.append({"command": s["command"], "status": "skipped"})
                continue
            r = _run_step(dev, s, prompt_rx)
            results.append(r)
            failed = r["status"] != "success"
    finally:
        _close(dev, clean=not failed)
    ok = all(r["status"] == "success" for r in results)
    print(json.dumps({"success": ok, "device": name, "device_prompt": dev.base_prompt,
                      "steps": results, "elapsed": round(time.monotonic() - t0, 3)}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
