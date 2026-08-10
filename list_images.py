import argparse
import json
import stat
import sys

import paramiko

# Lists files in a directory on the file server via SFTP, for populating
# a binary-selection dropdown. Read-only and fast (a single SFTP
# directory listing), so unlike the transfer service this runs
# synchronously — no detach/background-process handling needed here.
#
# Legacy IAG4 equivalent: `ls -l <dir>` (run locally, since IAG4 was
# co-located with the file server) parsed line-by-line with a TextFSM
# template (linuxLs) to extract filename + size. That template applied
# no extension filtering — it relied on the per-model subfolder already
# containing only relevant files. Since the file server is now a
# separate host, this uses paramiko's SFTP listdir_attr() instead, which
# returns structured filename/size/mtime directly — no regex parsing of
# `ls` output needed at all. An optional `extensions` filter is added
# here since the current design explicitly wants results filtered to
# binaries appropriate for the device's model, not just "whatever's in
# the folder" as the legacy template assumed.


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fs_host", required=True)
    parser.add_argument("--fs_user", required=True)
    parser.add_argument("--fs_password", required=True)
    parser.add_argument("--directory", required=True,
                         help="Full path on the file server to list, e.g. /data/iosimages/9K")
    parser.add_argument("--extensions", required=False, default="",
                         help="Comma-separated list of extensions to filter to, e.g. '.bin,.SPA.bin,.pkg'. "
                              "Omit to return every regular file in the directory.")
    return parser.parse_args()


def main():
    args = parse_args()

    result = {"success": False, "connected_to_file_server": False}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=args.fs_host,
            username=args.fs_user,
            password=args.fs_password,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        result["connected_to_file_server"] = True

        extensions = tuple(e.strip() for e in args.extensions.split(",") if e.strip())

        sftp = client.open_sftp()
        try:
            entries = sftp.listdir_attr(args.directory)
        finally:
            sftp.close()

        images = []
        for entry in entries:
            if not stat.S_ISREG(entry.st_mode):
                continue  # skip subdirectories
            if extensions and not entry.filename.endswith(extensions):
                continue
            images.append({
                "name": entry.filename,
                "size_bytes": entry.st_size,
                "modified": entry.st_mtime,
            })

        images.sort(key=lambda i: i["name"])
        result["images"] = images
        result["success"] = True

    except paramiko.AuthenticationException:
        result["error"] = "file_server_authentication_failed"
    except FileNotFoundError:
        result["error"] = f"directory_not_found: {args.directory}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
