import argparse
import json
import sys

from netmiko import ConnectHandler

# Runs a single CLI command against a network device via netmiko and
# returns its raw output. Added because GatewayManager.sendCommand /
# sendConfig require targeting a pre-registered gateway inventory, and
# this project doesn't have one set up yet — this service instead takes
# ad hoc host/user/password, consistent with gateway5-file-transfer and
# gateway5-list-images*, and matches the netmiko-based pattern already
# used by the real Fastiron driver on this cluster (see
# Ruckus/Fastiron/device-drivers/netmiko-python in cluster_1's config)
# rather than raw paramiko exec — Cisco IOS needs an interactive shell
# with prompt/paging handling, which is exactly what netmiko provides.
#
# Used for the two device-side steps in the image transfer workflow that
# don't fit the file-transfer/listing services: polling `dir
# flash:<filename>` until the copied file's size stops growing, and
# running `verify /md5 flash:<filename>` afterward.


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device_host", required=True)
    parser.add_argument("--device_user", required=True)
    parser.add_argument("--device_password", required=True)
    parser.add_argument("--device_type", required=False, default="cisco_ios",
                         help="netmiko device_type, e.g. cisco_ios, cisco_xe")
    parser.add_argument("--command", required=True)
    return parser.parse_args()


def main():
    args = parse_args()

    result = {"success": False, "connected": False}

    try:
        conn = ConnectHandler(
            device_type=args.device_type,
            host=args.device_host,
            username=args.device_user,
            password=args.device_password,
            timeout=15,
        )
        result["connected"] = True
        try:
            result["output"] = conn.send_command(args.command, read_timeout=30)
            result["success"] = True
        finally:
            conn.disconnect()

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result))


if __name__ == "__main__":
    main()
