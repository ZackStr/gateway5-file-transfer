import sys
import json
import shlex

import paramiko

# Expected stdin JSON:
# {
#   "file_server": {"host": "...", "user": "...", "password": "<resolved secret>"},
#   "device":      {"host": "...", "user": "...", "password": "<resolved secret>"},
#   "src": "/home/smarts/IOS/Cisco/9K/testfile.txt",
#   "dest": "flash:testfile.txt"
# }
#
# file_server/device passwords are expected to already be resolved plaintext
# by the time this script sees them (the caller passes them in via this
# cluster's gateway-secret mechanism at the workflow/task level, not here).
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


def main():
    data = json.load(sys.stdin)
    fs = data["file_server"]
    device = data["device"]
    src = data["src"]
    dest = data["dest"]

    print(f"Connecting to file server {fs['user']}@{fs['host']}...", file=sys.stderr)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    result = {"connected_to_file_server": False}

    try:
        client.connect(
            hostname=fs["host"],
            username=fs["user"],
            password=fs["password"],
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        result["connected_to_file_server"] = True

        print("Connected. Starting remote transfer process...", file=sys.stderr)
        stdin, stdout, stderr = client.exec_command(
            f"python3 -c {shlex.quote(REMOTE_SCRIPT)}"
        )

        payload = json.dumps(
            {
                "device_host": device["host"],
                "device_user": device["user"],
                "device_password": device["password"],
                "src": src,
                "dest": dest,
            }
        )
        stdin.write(payload)
        stdin.channel.shutdown_write()

        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()

        result["exit_code"] = exit_code
        result["remote_stdout"] = out
        result["remote_stderr"] = err

    except paramiko.AuthenticationException:
        result["error"] = "file_server_authentication_failed"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
