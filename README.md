# Gateway 5 File Transfer

An Itential Gateway 5 (IAG5) `python-script` service that pushes a file
from a Linux file server directly to a network device via SCP, without
proxying the transfer through the gateway itself.

## How it works

This script (`main.py`) runs **on the gateway**. It opens a single SSH
session to the file server, then executes a small Python process on the
file server over that session — that remote process performs the actual
SCP push to the device, so the data path is file-server → device directly.

The device credential is handed to the remote process over its stdin
(after the process has already started, via the encrypted SSH channel),
never as a command-line argument or environment variable on that host —
so it never appears in a process list or shell history on either host.

The device-facing transfer uses the `scp` package (the legacy SCP
protocol over a paramiko transport), not paramiko's own `SFTPClient`.
Cisco IOS devices generally only expose the legacy SCP protocol
(`ip scp server enable`), not an SFTP subsystem — SFTP would silently
work against a generic Linux test target but fail against real hardware.

## IAG service contract

- Non-secret inputs (`fs_host`, `fs_user`, `device_host`, `device_user`,
  `src`, `dest`) arrive as `--flag` CLI args, per the decorator schema in
  `services.yaml`.
- Secrets (`FS_PASSWORD`, `DEVICE_PASSWORD`) arrive as environment
  variables, injected by IAG from the service's `secrets:` block — never
  as CLI args.
- The script always prints one JSON object to stdout (`{"success": ...}`),
  and exits 0 for any handled result (success or failure); exit 1 is
  reserved for fatal setup errors (e.g. missing required secrets).

## Requirements

- **Gateway side** (`requirements.txt`): `paramiko`
- **File server side** (provisioned separately on whatever host is
  registered as the file server, not covered by this repo): `paramiko` +
  `scp`, since the remote leg executes there, not on the gateway

## Registering with IAG

See `services.yaml` for the decorator, repository, and service
definition. Import with:

```bash
iagctl db import services.yaml --validate
iagctl db import services.yaml
```

Create the two secrets separately (never commit real values):

```bash
iagctl create secret gateway5-file-transfer-fs-password --prompt-value
iagctl create secret gateway5-file-transfer-device-password --prompt-value
```

## Notes

- Invoked asynchronously via `GatewayManager.runService` — the platform
  returns a `job_id` immediately and the caller polls job status
  separately; this script runs synchronously start to finish, the
  platform handles the async contract around it.
- General-purpose enough to reuse for any file-server-to-device SCP push
  scenario, not tied to a specific device vendor beyond the SCP-vs-SFTP
  note above.
