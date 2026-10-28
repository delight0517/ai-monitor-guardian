# Broker security design

Status: bounded Windows named-pipe transport and user/logon SID checks are implemented and tested locally with a fake handler. Connect, request-read, and reply-write have configurable hard deadlines (50 ms–10 s). The injected handler runs synchronously and must remain bounded; no production handler exists. There is no installer, privileged service, production guest transport, or app runtime connection.

## Local API boundary

- Fixed endpoint: `\\.\pipe\OnlyChatAiDebugDisplay.v1` for the current consumer adapter. One newline-terminated UTF-8 JSON request and one reply per connection; 4096-byte body maximum. Reject multiple frames, extra bytes, unknown fields and unsupported versions.
- Operations are limited to `status`, `acquire`, `heartbeat`, and `release`. Requests carry request/run identifiers and explicit confirmation; they cannot carry shell commands, paths, executable names, arbitrary window handles, credentials, screenshots, prompts or chat text.
- `AuthenticatedNamedPipeServer` uses a protected DACL, rejects remote clients, uses overlapped I/O with bounded connect/read/write deadlines, reads one bounded message, then impersonates the client to compare user and logon SID before dispatch. It reverts before calling the injected handler. The package does not include an elevated service.
- User/logon SID checks identify the interactive account/session, not a particular executable. Any process running as that user in the same logon may connect. A production privileged broker needs an independently reviewed app/installer trust bootstrap; a request's `confirmed: true` field alone is not an authenticated user action.
- Local Windows tests cover allowed/mismatched SID paths, fake-handler request/response, stalled-client timeout, and stop cancellation during idle connect and stalled read. They do not validate a production service ACL, app-specific identity bootstrap, a hung production handler, install/uninstall, crash recovery, or guest operation.

## Isolated guest boundary

- Keep host-attached virtual display drivers disabled. They are ordinary Windows outputs and can affect primary-display selection, user windows and cursor behavior.
- Any future guest backend must target one explicitly configured disposable VM, with networking and host file integration disabled by default. Never accept VM names or command paths from the consumer request.
- A host/guest channel would need fresh run-bound authentication, strict message-size limits and replay protection. The guest must never send commands for the host to execute.
- On release or idle expiry, stop frame forwarding, disable the guest display lease, request normal guest shutdown and confirm the VM is Off. A forced power-off is not a normal success path.

## Required before operational use

Implement and review a trusted app/installer identity bootstrap, privilege separation, replay handling, broker crash recovery, guest readiness protocol, frame transport, graceful shutdown and uninstall path. Then verify them only in a disposable guest while asserting host display topology, foreground and user windows remain unchanged. Current fake-handler transport tests do not establish these runtime properties.
