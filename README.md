# Gateway 5 File Transfer

An Itential Gateway 5 (IAG5) `python-script` service that pushes a file
from a Linux file server directly to a network device via SCP — without
proxying the transfer through the gateway, and without blocking the
calling workflow for the transfer's full duration.

## Why this is async (a hard-learned lesson)

`GatewayManager.runService` — and the workflow task that calls it —
**waits for the invoked script to exit** before the task completes. An
earlier version of this repo ran the device-facing transfer
synchronously inside the script and waited for it to finish. That
"worked" for a small test file, but a 1GB file took ~4 minutes and the
calling workflow task blocked for the entire ~4 minutes; a real IOS
image would block for hours. That defeats the reason to build this as a
service in the first place instead of a blocking IAG task.

The fix: this script launches the actual transfer as a genuinely
**detached background process on the file server** (`setsid nohup ...
&`, with stdin/stdout/stderr all redirected away from the SSH session so
it survives after the session closes) and returns immediately, without
waiting for the transfer to finish.

**Determining completion is not this service's job.** The calling
workflow does that separately, by polling `dir flash:<filename>` on the
device itself until the file size stops growing — the same approach
already used for the SCP-based image transfer design. This service has
no job-status contract to poll; once it returns, the transfer is running
in the background and the workflow is expected to check on it via the
device, not via this service.

## How it works

This script runs **on the gateway**. It:

1. Opens one SSH session to the file server.
2. Computes the source file's MD5 there (`md5sum`) — fast, done before
   the transfer even begins.
3. Stages two files on the file server via SFTP: the transfer script
   itself, and a small JSON credential payload (`chmod 600`, deleted by
   that script immediately after it reads it — never argv, never a
   literal in a log). Filenames include a random suffix only to avoid
   collisions between concurrent transfers on the same file server (e.g.
   a batch upgrading several devices at once) — it isn't a job id
   exposed to the caller.
4. Launches that script as a fully detached background process and
   returns `{success, source_md5}` without waiting for it.

The backgrounded process does the actual device-facing push using the
`scp` package (SCP protocol over a paramiko transport) — not paramiko's
own `SFTPClient`. Cisco IOS devices generally only expose the legacy SCP
protocol (`ip scp server enable`), not an SFTP subsystem, so SFTP would
work against a generic Linux test target but silently fail against real
hardware. (SFTP is used here only to stage files on the file server
itself, which is a normal Linux box — not for the device-facing leg.)

On completion, the backgrounded process writes a debugging log to
`/tmp/.gw5-log-<suffix>.json` on the file server and deletes its own
script file — this is for manual troubleshooting only, nothing reads it
back automatically.

## IAG service contract

- All inputs, including `fs_password`/`device_password`, arrive as
  `--flag` CLI args per the decorator schema in `services.yaml` — the
  standard IAG python-script contract.
- **Passwords are dynamic per-call, not a static service-level secret
  binding.** Callers pass a resolved gateway-secret reference (e.g.
  `$GATEWAYSECRET_(name)`) as the value of `fs_password`/`device_password`
  at invocation time, so any registered secret can be used per call
  without re-importing the service. Trade-off: since these are decorator
  properties, the resolved plaintext briefly appears in the gateway's own
  process list (`ps aux`) while the script runs — chosen deliberately for
  per-call flexibility over the stricter guarantee a static `secrets:`
  env-var binding would give. (This only applies on the gateway; the
  device credential's second hop, from the file server into the
  backgrounded transfer process, goes through a file, never argv there.)
- The script always prints one JSON object to stdout, and exits 0 for
  any handled result (success or failure); exit 1 is reserved for fatal
  setup errors.

## Output

```json
{
  "success": true,
  "connected_to_file_server": true,
  "source_md5": "d41d8cd98f00b204e9800998ecf8427e",
  "source_size_bytes": 1073741824
}
```

`source_size_bytes` comes from an SFTP `stat()` call on the source file
(essentially free — reuses the SFTP session already open for staging),
useful for a caller that needs to check free space on the destination
before/after the transfer without a separate lookup.

If the source file can't be hashed (e.g. it doesn't exist), the script
fails fast with `success: false` before attempting anything else — no
background process gets launched in that case.

## Requirements

- **Gateway side** (`requirements.txt`): `paramiko`
- **File server side** (provisioned separately on whatever host is
  registered as the file server, not covered by this repo): `paramiko` +
  `scp`, plus a working SFTP subsystem (default on most Linux sshd
  configs) and `setsid`/`nohup` (present on virtually all Linux distros)

## Registering with IAG

See `services.yaml` for the decorator, repository, and service
definition. Import from the repo directly:

```bash
iagctl db import services.yaml --repository <this-repo-url> --reference main --validate
iagctl db import services.yaml --repository <this-repo-url> --reference main
```

No service-level secrets need to be created — passwords are supplied per
call (see above), referencing whatever secrets are already registered on
the target cluster.

## Notes

- General-purpose enough to reuse for any file-server-to-device SCP push
  scenario, not tied to a specific device vendor beyond the SCP-vs-SFTP
  note above.
