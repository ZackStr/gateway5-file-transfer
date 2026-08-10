import argparse
import json
import os
import shlex
import sys

import paramiko

# IAG python-script contract:
#   - non-secret inputs arrive as --flag CLI args (decorator schema properties)
#   - secrets arrive as env vars, injected by IAG from the service's
#     `secrets:` block — never as CLI args
#   - always print a single JSON object to stdout; exit 0 for any handled
#     result (success or failure), exit 1 only for fatal setup errors
#
# Architecture: this script runs on the GATEWAY. It opens one SSH session to
# the file server, then runs a small remote Python process ON the file
# server to perform the actual device-facing transfer — keeping the data
# path file-server -> device direct, never relayed through the gateway.
# The device credential is handed to that remote process over its stdin
# (post-exec, over the already-encrypted channel), never as a command-line
# argument, so it never appears in a process list or shell history on
# either host.
#
# The remote leg uses the `scp` package (SCP protocol over a paramiko
# transport), not paramiko's own SFTPClient — Cisco IOS devices generally
# only expose the legacy SCP protocol (`ip scp server enable`), not an SFTP
# subsystem, so SFTP would work against a generic Linux test target but
# silently fail against real hardware.

REMOTE_SCRIPT = r"""
import sys, json
import paramiko
from scp import SCPClient

data = json.load(sys.stdin)

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
except Exception as e:
    result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
finally:
    scp.close()
    client.close()

print(json.dumps(result))
"""


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fs_host", required=True)
    parser.add_argument("--fs_user", required=True)
    parser.add_argument("--device_host", required=True)
    parser.add_argument("--device_user", required=True)
    parser.add_argument("--src", required=True)
    parser.add_argument("--dest", required=True)
    return parser.parse_args()


def main():
    args = parse_args()

    fs_password = os.environ.get("FS_PASSWORD")
    device_password = os.environ.get("DEVICE_PASSWORD")
    if not fs_password or not device_password:
        print(json.dumps({
            "success": False,
            "error": "FS_PASSWORD and DEVICE_PASSWORD env vars required",
        }))
        sys.exit(1)

    print(f"Connecting to file server {args.fs_user}@{args.fs_host}...", file=sys.stderr)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    result = {"success": False, "connected_to_file_server": False}

    try:
        client.connect(
            hostname=args.fs_host,
            username=args.fs_user,
            password=fs_password,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        result["connected_to_file_server"] = True

        print("Connected. Starting remote transfer process...", file=sys.stderr)
        stdin, stdout, stderr = client.exec_command(
            f"python3 -c {shlex.quote(REMOTE_SCRIPT)}"
        )

        payload = json.dumps({
            "device_host": args.device_host,
            "device_user": args.device_user,
            "device_password": device_password,
            "src": args.src,
            "dest": args.dest,
        })
        stdin.write(payload)
        stdin.channel.shutdown_write()

        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()

        result["exit_code"] = exit_code
        result["remote_stdout"] = out
        result["remote_stderr"] = err
        result["success"] = exit_code == 0

    except paramiko.AuthenticationException:
        result["error"] = "file_server_authentication_failed"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
