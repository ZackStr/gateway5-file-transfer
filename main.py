import argparse
import json
import shlex
import sys
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


def get_source_md5(client, src):
    stdin, stdout, stderr = client.exec_command(f"md5sum {shlex.quote(src)}")
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if exit_code != 0:
        raise RuntimeError(err or out or f"md5sum exited {exit_code}")
    return out.split()[0]


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


def main():
    args = parse_args()

    result = {"success": False, "connected_to_file_server": False}
    client = connect_fs(args)

    try:
        result["connected_to_file_server"] = True

        print("Computing source file MD5 on file server...", file=sys.stderr)
        result["source_md5"] = get_source_md5(client, args.src)

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
        launch_cmd = (
            f"setsid nohup python3 {script_path} {payload_path} {log_path} "
            f"> /dev/null 2>&1 < /dev/null &"
        )
        stdin, stdout, stderr = client.exec_command(launch_cmd)
        launch_exit_code = stdout.channel.recv_exit_status()

        result["success"] = launch_exit_code == 0

    except paramiko.AuthenticationException:
        result["error"] = "file_server_authentication_failed"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
