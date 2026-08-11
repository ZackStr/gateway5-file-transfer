import argparse
import json
import posixpath
import stat
import sys

import paramiko

# Recursively lists every file under a base path on the file server in a
# single SSH/SFTP session — one round trip for every model's binaries at
# once, instead of one call per model subfolder (gateway5-list-images).
#
# This mirrors a common legacy pattern: an `ls`-tree-style listing of the
# whole image root in one shot, rather than querying each device-family
# subfolder separately, to cut down on file-server round trips. Kept as
# a separate service from gateway5-list-images so callers that only need
# one exact folder aren't paying for a full tree walk.
#
# Each result includes which subfolder (model family) it came from, so
# the calling workflow can bucket/filter by model without any further
# file-server queries.


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fs_host", required=True)
    parser.add_argument("--fs_user", required=True)
    parser.add_argument("--fs_password", required=True)
    parser.add_argument("--base_path", required=True,
                         help="Root directory to walk recursively, e.g. /home/smarts/IOS/Cisco/ (the binaryPath env var)")
    parser.add_argument("--extensions", required=False, default="",
                         help="Comma-separated list of extensions to filter to, e.g. '.bin,.SPA.bin,.pkg'. "
                              "Omit to return every regular file found.")
    return parser.parse_args()


def walk(sftp, base_path, current_path, extensions, images):
    for entry in sftp.listdir_attr(current_path):
        full_path = posixpath.join(current_path, entry.filename)
        if stat.S_ISDIR(entry.st_mode):
            walk(sftp, base_path, full_path, extensions, images)
        elif stat.S_ISREG(entry.st_mode):
            if extensions and not entry.filename.endswith(extensions):
                continue
            folder = posixpath.relpath(current_path, base_path)
            images.append({
                "folder": "" if folder == "." else folder,
                "name": entry.filename,
                "path": full_path,
                "size_bytes": entry.st_size,
                "modified": entry.st_mtime,
            })


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
            images = []
            walk(sftp, args.base_path.rstrip("/"), args.base_path.rstrip("/"), extensions, images)
        finally:
            sftp.close()

        images.sort(key=lambda i: (i["folder"], i["name"]))
        result["images"] = images
        result["success"] = True

    except paramiko.AuthenticationException:
        result["error"] = "file_server_authentication_failed"
    except FileNotFoundError:
        result["error"] = f"base_path_not_found: {args.base_path}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
