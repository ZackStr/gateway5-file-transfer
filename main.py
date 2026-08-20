import argparse
import base64
import json
import shlex
import sys
import time
import uuid

import paramiko

# IAG python-script contract:
#   - all decorator schema properties arrive as --flag CLI args, including
#     fs_password/device_password here — this service deliberately takes
#     passwords as dynamic per-call inputs (a resolved gateway-secret
#     reference the caller substitutes before invocation) rather than a
#     static service-level secret binding, trading away "never in argv on
#     the gateway" for the ability to pick any registered secret per call
#     without a re-import. See services.yaml property descriptions.
#   - always print a single JSON object to stdout; exit 0 for any handled
#     result (success or failure), exit 1 only for fatal setup errors
#
# IMPORTANT — async architecture:
#   GatewayManager.runService (and the underlying workflow task) WAITS for
#   this script to exit before the workflow task completes. An earlier
#   version of this script called the device-facing transfer synchronously
#   and waited for it to finish — for a 1GB file that meant the workflow
#   task blocked for ~4 minutes, and would block for hours on a real IOS
#   image. That defeats the entire point of building this as an async
#   service instead of a blocking IAG task.
#
#   Fix: launch the transfer as a genuinely detached background process on
#   the FILE SERVER (setsid + nohup, redirected fds, so it survives after
#   we close the SSH session) and return immediately. Completion is
#   determined by the calling workflow separately, by polling `dir
#   flash:<filename>` on the device itself until the file size stops
#   growing — not by anything this service reports back, so there is no
#   job-status contract here to poll.
#
# The device-facing transfer uses the `scp` package (SCP protocol over a
# paramiko transport), not paramiko's own SFTPClient — Cisco IOS devices
# generally only expose the legacy SCP protocol (`ip scp server enable`),
# not an SFTP subsystem, so SFTP would work against a generic Linux test
# target but silently fail against real hardware. The file server itself
# is assumed to be a normal Linux box with SFTP available (used here only
# to stage the payload/script files, not for the device-facing transfer).
#
# IMPORTANT — fail fast, before forking:
#   Once the background transfer is launched, this service has no way to
#   observe whether it succeeds or fails short of the file server's own
#   debug log (see REMOTE_TRANSFER_SCRIPT's log_path) — there is no
#   job-status contract, by design (see above). That makes it critical to
#   catch everything catchable *before* the fork, synchronously, so the
#   common failure modes (missing source file, source file server unreadable,
#   device unreachable/wrong credentials) show up immediately in this
#   script's own JSON result instead of requiring someone to SSH into the
#   file server and go hunting for a log file after the fact (this bit us
#   once already: a real device on a different network segment than the
#   file server timed out mid-transfer with the failure only visible in
#   that log). Order: source file exists -> source MD5 computable -> file
#   server can actually SSH to the device -> only then stage + fork.

REMOTE_TRANSFER_SCRIPT = r"""
import sys, json, os

payload_path = sys.argv[1]
log_path = sys.argv[2]

with open(payload_path) as f:
    data = json.load(f)
os.remove(payload_path)

import paramiko
from scp import SCPClient

try:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=data["device_host"],
        username=data["device_user"],
        password=data["device_password"],
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    scp = SCPClient(client.get_transport())
    try:
        scp.put(data["src"], data["dest"])
        result = {"status": "ok"}
    finally:
        scp.close()
        client.close()
except Exception as e:
    result = {"status": "error", "error": f"{type(e).__name__}: {e}"}

# Debugging aid only — nothing polls this file back. Completion is
# determined by the calling workflow via `dir flash:` on the device.
with open(log_path, "w") as f:
    json.dump(result, f)

try:
    os.remove(sys.argv[0])
except Exception:
    pass
"""

# Run on the FILE SERVER (not the gateway) via exec_command, before the
# background transfer is forked -- proves the file server can actually
# reach and authenticate to the device over SSH. Streamed directly over the
# exec_command stdin channel (python3 -) instead of staged as a file first
# -- this step used to SFTP-write a script + payload file to /tmp, which
# failed outright (bare paramiko OSError('Failure')) the one time a real
# file server's /tmp was completely full, well before ever attempting the
# device connection. Streaming means this check never touches disk at all.
# The credential payload is base64-embedded directly in the script text
# (not JSON-embedded in a quoted literal) so arbitrary password content
# can never break out of the string literal -- base64's alphabet has no
# quotes/backslashes to escape. Never shows up in the file server's process
# list or shell history either way, since it's channel data, not argv.
DEVICE_PRECHECK_SCRIPT = r"""
import base64, json
import paramiko

data = json.loads(base64.b64decode("__PAYLOAD_B64__").decode())

try:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        hostname=data["device_host"],
        username=data["device_user"],
        password=data["device_password"],
        timeout=10,
        look_for_keys=False,
        allow_agent=False,
    )
    c.close()
    print(json.dumps({"ok": True}))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
"""


def connect_fs(args):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.fs_host,
        username=args.fs_user,
        password=args.fs_password,
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


class TransferPrecheckError(Exception):
    """Raised for any fail-fast check that must stop us before forking the transfer."""

    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def stat_source(sftp, src):
    try:
        return sftp.stat(src)
    except FileNotFoundError as e:
        raise TransferPrecheckError("source_file_not_found", f"{src} does not exist on the file server") from e
    except IOError as e:
        raise TransferPrecheckError("source_file_not_found", f"{src}: {e}") from e


def get_source_md5(client, src):
    stdin, stdout, stderr = client.exec_command(f"md5sum {shlex.quote(src)}")
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if exit_code != 0:
        raise TransferPrecheckError("source_md5_failed", err or out or f"md5sum exited {exit_code}")
    return out.split()[0]


def precheck_device_ssh(client, args):
    """Proves the file server can SSH+auth to the device, run from the file
    server itself (not the gateway) since that's the network path that
    matters for the actual transfer. Raises TransferPrecheckError on any
    failure so we never fork a transfer we already know can't connect."""
    payload = json.dumps({
        "device_host": args.device_host,
        "device_user": args.device_user,
        "device_password": args.device_password,
    })
    payload_b64 = base64.b64encode(payload.encode()).decode()
    script = DEVICE_PRECHECK_SCRIPT.replace("__PAYLOAD_B64__", payload_b64)

    stdin, stdout, stderr = client.exec_command("python3 -")
    stdin.write(script)
    stdin.channel.shutdown_write()
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()

    if exit_code != 0 or not out:
        raise TransferPrecheckError("device_ssh_precheck_failed", err or out or f"precheck exited {exit_code}")
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as e:
        raise TransferPrecheckError("device_ssh_precheck_failed", f"unparseable precheck output: {out}") from e
    if not parsed.get("ok"):
        raise TransferPrecheckError("device_ssh_precheck_failed", parsed.get("error", "unknown precheck failure"))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fs_host", required=True)
    parser.add_argument("--fs_user", required=True)
    parser.add_argument("--fs_password", required=True)
    parser.add_argument("--device_host", required=True)
    parser.add_argument("--device_user", required=True)
    parser.add_argument("--device_password", required=True)
    parser.add_argument("--src", required=True)
    parser.add_argument("--dest", required=True)
    return parser.parse_args()


def emit(result):
    # Booleans are deliberately serialized as the strings "true"/"false", not
    # JSON booleans. The calling workflow's `evaluation` task compares this
    # service's `success` field directly -- and on this platform, an
    # `evaluation` operand that resolves to a genuine JSON boolean silently
    # takes the failure branch (empty `outgoing`, no error surfaced anywhere)
    # regardless of what it's compared against. String-to-string comparison
    # is what already works elsewhere in that workflow (size/MD5 checks), so
    # every top-level boolean here is stringified to match, not just `success`.
    stringified = {k: ("true" if v is True else "false" if v is False else v) for k, v in result.items()}
    print(json.dumps(stringified))


def main():
    args = parse_args()

    result = {
        "success": False,
        "connected_to_file_server": False,
        "source_exists": False,
        "device_reachable": False,
    }

    try:
        client = connect_fs(args)
    except paramiko.AuthenticationException:
        result["error"] = "file_server_authentication_failed"
        emit(result)
        return
    except Exception as e:
        result["error"] = f"file_server_connection_failed: {type(e).__name__}: {e}"
        emit(result)
        return

    try:
        result["connected_to_file_server"] = True

        # Best-effort housekeeping: the background transfer script leaves its
        # own debug log (.gw5-log-*.json) behind indefinitely by design --
        # nothing polls it back automatically, it's there for a human to
        # inspect after a failure -- so left alone it accumulates forever on
        # a shared file server. Sweep anything older than an hour (plenty of
        # time to have grabbed a real failure's log) on every invocation
        # instead. Also catches any precheck helper file that failed to
        # clean up after itself. Never blocks a real transfer on this.
        try:
            stdin, stdout, stderr = client.exec_command(
                "find /tmp -maxdepth 1 -name '.gw5-*' -mmin +60 -delete"
            )
            stdout.channel.recv_exit_status()
        except Exception:
            pass

        print("Checking source file exists on file server...", file=sys.stderr)
        sftp = client.open_sftp()
        try:
            stat = stat_source(sftp, args.src)
            result["source_exists"] = True
            result["source_size_bytes"] = stat.st_size
        finally:
            sftp.close()

        print("Computing source file MD5 on file server...", file=sys.stderr)
        result["source_md5"] = get_source_md5(client, args.src)

        print("Checking file server can SSH to the device...", file=sys.stderr)
        precheck_device_ssh(client, args)
        result["device_reachable"] = True

        # Random suffix here is only to avoid filename collisions between
        # concurrent transfers on the same file server (e.g. a batch
        # upgrading several devices at once) — not a job id the caller
        # needs to track; nothing reports back through it.
        suffix = uuid.uuid4().hex
        script_path = f"/tmp/.gw5-transfer-{suffix}.py"
        payload_path = f"/tmp/.gw5-payload-{suffix}.json"
        log_path = f"/tmp/.gw5-log-{suffix}.json"

        print("Staging transfer script and credential payload on file server...", file=sys.stderr)
        sftp = client.open_sftp()
        try:
            with sftp.open(script_path, "w") as f:
                f.write(REMOTE_TRANSFER_SCRIPT)
            sftp.chmod(script_path, 0o700)

            payload = json.dumps({
                "device_host": args.device_host,
                "device_user": args.device_user,
                "device_password": args.device_password,
                "src": args.src,
                "dest": args.dest,
            })
            with sftp.open(payload_path, "w") as f:
                f.write(payload)
            sftp.chmod(payload_path, 0o600)
        finally:
            sftp.close()

        print("Launching detached background transfer...", file=sys.stderr)
        # `setsid` is Linux-only (util-linux) -- absent on macOS entirely, so
        # a file server that happens to be a Mac would silently never launch
        # anything: "setsid: command not found" inside a backgrounded `&`
        # command doesn't surface as a nonzero exit code here, since `&`
        # itself always "succeeds" at *starting* the background job
        # regardless of whether the child command that follows it can even
        # be found. Confirmed 2026-08-19 -- every prior run against this
        # laptop-as-file-server left orphaned .gw5-payload-*/.gw5-transfer-*
        # files behind with no .gw5-log-* ever written, meaning the script
        # never even got as far as reading its own payload. Plain `nohup
        # ... &` (no setsid) is portable across Linux and macOS and is
        # sufficient to survive this SSH session closing.
        launch_cmd = (
            f"nohup python3 {script_path} {payload_path} {log_path} "
            f"> /dev/null 2>&1 < /dev/null & echo $!"
        )
        stdin, stdout, stderr = client.exec_command(launch_cmd)
        launch_exit_code = stdout.channel.recv_exit_status()
        pid_str = stdout.read().decode(errors="replace").strip()

        # launch_exit_code can't catch a failure in the backgrounded command
        # itself (see above) -- explicitly verify the process is actually
        # alive after a brief moment, so a missing interpreter/dependency on
        # the file server is caught here instead of silently vanishing.
        time.sleep(0.5)
        _, check_stdout, _ = client.exec_command(f"kill -0 {pid_str} 2>/dev/null && echo alive || echo dead")
        alive = check_stdout.read().decode(errors="replace").strip() == "alive"

        result["success"] = launch_exit_code == 0 and pid_str.isdigit() and alive
        if not result["success"]:
            result["error"] = f"background_launch_failed: pid={pid_str!r} alive={alive} exit_code={launch_exit_code}"

    except TransferPrecheckError as e:
        result["error"] = f"{e.code}: {e.detail}"
    except paramiko.AuthenticationException:
        result["error"] = "file_server_authentication_failed"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()

    emit(result)


if __name__ == "__main__":
    main()
