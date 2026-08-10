# Gateway 5 File Transfer

An Itential Gateway 5 `python-script` service that pushes a file from a
Linux file server directly to a network device via SCP, without proxying
the transfer through the gateway itself.

## How it works

This script (`main.py`) runs **on the gateway**. It opens a single SSH
session to the file server, then executes a small Python process on the
file server over that session — that remote process performs the actual
SCP push to the device, so the data path is file-server → device directly.

The device credential is handed to the remote process over its stdin
(after the process has already started, via the encrypted SSH channel),
never as a command-line argument or environment variable — so it never
appears in a process list or shell history on either host.

The device-facing transfer uses the `scp` package (the legacy SCP
protocol over a paramiko transport), not paramiko's own `SFTPClient`.
Cisco IOS devices generally only expose the legacy SCP protocol
(`ip scp server enable`), not an SFTP subsystem — SFTP would silently
work against a generic Linux test target but fail against real hardware.

## Requirements

- **Gateway side** (this repo, `requirements.txt`): `paramiko`
- **File server side** (not covered by this repo — provision separately
  on whatever host is registered as the file server): `paramiko` + `scp`,
  since the remote leg executes there, not on the gateway

## Input schema

See `decorator.json`. Expected input:

```json
{
  "file_server": {"host": "...", "user": "...", "password": "<resolved secret>"},
  "device":      {"host": "...", "user": "...", "password": "<resolved secret>"},
  "src": "/path/on/file/server/image.bin",
  "dest": "flash:image.bin"
}
```

Both `password` fields are expected to be resolved plaintext by the time
this script sees them — the caller is responsible for passing them in via
this cluster's gateway-secret mechanism (e.g. a `$GATEWAYSECRET_(name)`
reference resolved by the platform before invocation), not as literal
values.

## Notes

- This is invoked asynchronously via `GatewayManager.runService` — the
  platform returns a `job_id` immediately and the caller polls job status
  separately; this script does not need to implement its own async
  contract, it can run synchronously start to finish.
- General-purpose enough to reuse for any file-server-to-device SCP push
  scenario, not tied to a specific device vendor beyond the SCP-vs-SFTP
  note above.
