# Gateway 5 File Transfer

Four Itential Gateway 5 (IAG5) `python-script` services for file-server-
to-device IOS image workflows and interactive device commands:

- **`gateway5-file-transfer`** — pushes a file from a Linux file server
  directly to a network device via SCP, without proxying through the
  gateway and without blocking the calling workflow for the transfer's
  full duration.
- **`gateway5-list-images`** — lists files in one exact directory on the
  file server (name, size, mtime), for populating a binary-selection
  dropdown.
- **`gateway5-list-images-tree`** — recursively lists every file under a
  base path in one call, covering every model's subfolder at once
  instead of one file-server round trip per model.

- **`gateway5-run-prompt-commands`** — runs CLI commands on a device and
  answers their interactive prompts (text or a bare Enter), for commands
  such as `install remove inactive` or `copy` that stop and ask.

Plain device-side CLI (`dir flash:`, `verify /md5`, etc.) is intentionally
**not** covered by a service in this repo — that's handled by a native
Itential task (`sendCommand`) against the device. The one exception is
commands that need their prompts answered, which the native task cannot do.

## gateway5-file-transfer

### Why this is async (a hard-learned lesson)

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

### How it works

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

### IAG service contract

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

### Output

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

### Requirements

- **Gateway side** (`requirements.txt`): `paramiko`, `scp`
- **File server side** (provisioned separately on whatever host is
  registered as the file server, not covered by this repo -- this
  script only ever installs dependencies into the *gateway's* per-service
  venv, never onto the file server itself): `paramiko`, plus a working
  SFTP subsystem (default on most Linux sshd configs) and `nohup`
  (present on virtually all Linux distros).

  **This is easy to miss when onboarding a new file server** -- the
  device-reachability precheck runs a small script on the file server's
  own `python3`, streamed over the SSH session, not the gateway's
  provisioned environment. Confirmed twice in practice: the original
  file server had `paramiko` already present, but a second file server
  onboarded later did not, and failed with `ModuleNotFoundError: No
  module named 'paramiko'` inside the precheck (visible in the service's
  `error` field as `device_ssh_precheck_failed: ... ModuleNotFoundError:
  No module named 'paramiko'`) -- easy to misread as a connectivity
  problem rather than a missing dependency, since it surfaces through
  the same `device_reachable: false` field a real timeout would.

  **Before pointing this service at any new file server, verify first:**
  ```
  ssh <fs_user>@<fs_host> "python3 -c 'import paramiko; print(paramiko.__file__)'"
  ```
  If that fails, install it on the file server (not the gateway):
  ```
  ssh <fs_user>@<fs_host> "pip3 install --user paramiko"
  ```
  `--user` avoids needing root/sudo on the file server for a service
  account that likely doesn't have write access to the system
  site-packages directories.

## gateway5-list-images

Lists files in a directory on the file server via SFTP
(`listdir_attr()`), returning filename, size, and mtime per entry —
structured data, no `ls`/regex parsing involved. Runs synchronously
(a directory listing is fast regardless of size) — no async/background
handling needed here, unlike the transfer service.

Legacy IAG4 equivalent: ran `ls -l <dir>` locally (IAG4 was co-located
with the file server) and parsed it line-by-line with a TextFSM template
that applied no extension filtering, relying on the per-model subfolder
already containing only relevant files. This version adds an optional
`extensions` filter since the file server is now a separate host and the
design calls for results filtered to binaries appropriate for the
device's model, not just "whatever's in the folder."

### Output

```json
{
  "success": true,
  "connected_to_file_server": true,
  "images": [
    {"name": "asr1000-universalk9.16.09.02.SPA.bin", "size_bytes": 529477632, "modified": 1729302480.0}
  ]
}
```

### Inputs

- `fs_host` / `fs_user` / `fs_password` — same connection pattern as
  `gateway5-file-transfer` (dynamic gateway-secret reference for the
  password).
- `directory` — full path to list, e.g. `/data/iosimages/9K`. Mapping a
  device model to a folder name is the calling workflow's job, not this
  service's — kept generic/reusable on purpose.
- `extensions` (optional) — comma-separated list, e.g.
  `.bin,.SPA.bin,.pkg`. Omit to return every regular file.

## gateway5-list-images-tree

Same idea as `gateway5-list-images`, but walks every subfolder under a
base path recursively in a single SSH/SFTP session, instead of one call
per model-family folder. Matches a common legacy pattern (an `ls`-tree-
style listing of the whole image root at once) to cut down on
file-server round trips — useful when a batch spans multiple device
models and you'd otherwise need one `gateway5-list-images` call per
model.

Each result includes which subfolder it came from, so the calling
workflow can bucket/filter by model without any further file-server
queries.

### Output

```json
{
  "success": true,
  "connected_to_file_server": true,
  "images": [
    {"folder": "9K", "name": "asr1000-universalk9.16.09.02.SPA.bin", "path": "/home/smarts/IOS/Cisco/9K/asr1000-universalk9.16.09.02.SPA.bin", "size_bytes": 529477632, "modified": 1729302480.0},
    {"folder": "3850", "name": "cat3k_caa-universalk9.16.12.05.SPA.bin", "path": "/home/smarts/IOS/Cisco/3850/cat3k_caa-universalk9.16.12.05.SPA.bin", "size_bytes": 467250627, "modified": 1698500880.0}
  ]
}
```

### Inputs

- `fs_host` / `fs_user` / `fs_password` — same connection pattern as the
  other two services.
- `base_path` — root directory to walk, e.g. `/home/smarts/IOS/Cisco/`
  (the `binaryPath` env var value stored per-region on the
  `IOSdeviceUpgrade` trigger).
- `extensions` (optional) — same filtering behavior as
  `gateway5-list-images`.

## gateway5-run-prompt-commands

Runs one or more CLI commands over a single SSH session and answers the
prompts each command raises. The native `sendCommand` task can't do this:
it sends a command and returns, so a `[y/n]` or `[confirm]` prompt is left
unanswered until the device times it out.

**No credentials are passed in.** Call it with `GatewayManager.runService`
and the target `inventory` (`[{"inventory": "<name>", "nodeNames": ["<device>"]}]`).
Gateway5 pipes the node to the script on stdin (`InventoryInfo` JSON), so
host, user, password and platform come from Inventory Manager.

### Inputs

One input, `steps`, a JSON array run in order:

```json
{"steps": [
  {"command": "install remove inactive",
   "prompts": [{"expect": "\\[y/n\\]", "respond": "y"}],
   "timeout": 300,
   "expect_output": "SUCCESS: install_remove",
   "fail_on": ["User Rejected", "FAILED"]},
  {"command": "show version"}
]}
```

| Field | Meaning |
|---|---|
| `command` | CLI command to send (required) |
| `prompts[].expect` | Regex matched against the **end** of the output produced since the last answer |
| `prompts[].respond` | Text to send. `""` presses Enter with nothing typed (required, so an Enter is always deliberate) |
| `prompts[].optional` | `true` if the prompt may not appear |
| `prompts[].sensitive` | `true` to hide the response text in the result |
| `timeout` | Seconds allowed for the step (default 120) |
| `fail_on` | Optional. Extra regexes that fail the step when found in its output. A line starting with `%` (Invalid/Unknown/Ambiguous/Incomplete/Error) always fails it |
| `expect_output` | Optional. A regex, or a list that must all match, that the output has to contain once the device prompt returns; if not, the step fails. Omit for no outcome check |

### Behaviour

- Prompts are matched **in any order** and each is answered at most once.
- Only prompts listed for that step are ever answered. If the device stops
  at anything else, the step times out and **nothing is sent**.
- A step ends when the device's normal CLI prompt returns. A required
  prompt that never appeared fails the step.
- `fail_on` and `expect_output` are both optional and independent: a step
  runs the same with either, both or neither. `fail_on` says what must NOT
  be in the output, `expect_output` says what must be.
- The first failed step stops the run; later steps are `skipped`.
- After a failed step the connection is dropped without sending anything.
  A normal disconnect types `exit`, and some prompts (a `delete`'s
  `[confirm]`) accept that as an answer — confirmed on a Catalyst 8000V,
  where it deleted the file.
- Don't use a catch-all expect (e.g. `\\?`) with an empty response: it would
  also confirm prompts such as `Proceed with reload? [confirm]`.

### Output

One JSON object: `success`, `device`, `elapsed`, and per step `command`,
`status` (`success`/`failed`/`skipped`), `error`, `output`,
`prompts_answered` (`expect`, `sent`) and `elapsed`. Exit code is 0 only
when every step succeeded.

## Registering with IAG

See `services.yaml` for all four decorators, the repository, and all
four service definitions. Import from the repo directly:

```bash
iagctl db import services.yaml --repository <this-repo-url> --reference main --validate
iagctl db import services.yaml --repository <this-repo-url> --reference main
```

No service-level secrets need to be created for any of them. The file
services take passwords per call, referencing secrets already registered
on the target cluster; `gateway5-run-prompt-commands` takes none and gets
its credentials from the targeted inventory node.

## Notes

- General-purpose enough to reuse for any file-server-to-device SCP push
  or file-listing scenario, not tied to a specific device vendor beyond
  the SCP-vs-SFTP note above.
