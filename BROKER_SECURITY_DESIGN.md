# Broker security design

Status: protocol and design only. There is no named-pipe server, installer, service, guest transport, or authenticated runtime in this repository yet.

## Local API boundary

- Fixed endpoint: `\\.\pipe\OnlyChatAiDebugDisplay.v1` for the current consumer adapter. One newline-terminated UTF-8 JSON request and one reply per connection; 4096-byte body maximum. Reject multiple frames, extra bytes, unknown fields and unsupported versions.
- Operations are limited to `status`, `acquire`, `heartbeat`, and `release`. Requests carry request/run identifiers and explicit confirmation; they cannot carry shell commands, paths, executable names, arbitrary window handles, credentials, screenshots, prompts or chat text.
- A future broker must use a protected local-only pipe DACL and validate the expected user and logon SID for every connection. Impersonation must be bounded to identity inspection and reverted before privileged work. Enforce replay/idempotency checks and one active lease.
- A protocol parser is not authentication. Do not expose the candidate as available until server-side identity, ACL, timeout, crash recovery and stop/start race behavior are implemented and reviewed.

## Isolated guest boundary

- Keep host-attached virtual display drivers disabled. They are ordinary Windows outputs and can affect primary-display selection, user windows and cursor behavior.
- Any future guest backend must target one explicitly configured disposable VM, with networking and host file integration disabled by default. Never accept VM names or command paths from the consumer request.
- A host/guest channel would need fresh run-bound authentication, strict message-size limits and replay protection. The guest must never send commands for the host to execute.
- On release or idle expiry, stop frame forwarding, disable the guest display lease, request normal guest shutdown and confirm the VM is Off. A forced power-off is not a normal success path.

## Required before operational use

Implement and review the pipe DACL/SID validation, privilege separation, replay handling, broker crash recovery, guest readiness protocol, frame transport, graceful shutdown and uninstall path. Then verify them only in a disposable guest while asserting host display topology, foreground and user windows remain unchanged. None of these runtime checks are represented by the current parser tests.
