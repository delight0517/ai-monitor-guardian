# AI Monitor Guardian

**Experimental Windows library for isolated AI desktop QA.** This project is not a ready-to-install virtual monitor manager. It does not currently activate a host virtual display, launch a VM, capture a real guest screen, or connect a live Only Chat installation.

The safe direction is to keep AI UI work inside a disposable, private environment and leave the host's display topology, cursor, and ordinary apps alone. A host-attached VDD is still a normal Windows display and can become the primary output or receive user windows. This project therefore keeps its host display adapter inert.

## What is in this source release

- Versioned display-session and output-policy contracts with fail-closed behavior.
- A private Win32 desktop/process-job helper. It isolates focus; it is **not** a security boundary for files, network access, credentials, or account data.
- Reconnect placement journaling and policy helpers. They do not run a global watcher or move arbitrary windows.
- A fake-backend Hyper-V guest lifecycle contract with idle expiry and graceful-stop ordering.
- A bounded, newline-framed local broker protocol that accepts only `status`, `acquire`, `heartbeat`, and `release` actions. A parser is not an authenticated broker.
- A one-module-at-a-time evolution host and a cross-process feature-claim coordinator for cooperating source editors. Claims do not enforce filesystem permissions and do not merge competing edits.
- Source-only experimental guest display driver seeds. They are not built, signed, installed, or runtime-validated.

## Current runtime status

The manager package is at **0.7.0**. The test suite exercises policy and lifecycle code with synthetic inputs and fake backends; selected tests create and close a disposable private desktop process on Windows. There is no installed manager service, authenticated named-pipe server, bootable guest image, guest driver installation, or live app-to-screen session in this release.

The Only Chat consumer is maintained in a separate application source tree and is not included here. The protocol contract is documented for integration; the currently prepared consumer package is only a source candidate and is not installed into the running application.

## Run the source tests

Requires Python 3.10+.

```powershell
python -m unittest discover -s tests -v
```

The tests do not enable a host VDD, change monitor topology, start a Hyper-V VM, or touch user windows. Driver build and hardware/guest tests are not implied by a passing Python suite.

## Safety boundaries

- Do not install the experimental display-driver sources on a host Windows installation.
- Do not use `DisplaySwitch`, change the primary display, or move arbitrary user windows as part of a QA setup.
- A VM is not isolated merely because it has a virtual screen. Keep its network and host integration features disabled unless a reviewed workflow explicitly requires them.
- The named-pipe server, client authentication/DACL, guest transport, screenshot forwarding, and real start/heartbeat/stop path remain future work.
- Treat errors and cleanup timeouts as visible failures; never report a display as stopped until its owner confirms shutdown.

## Development model

See [MODULE_MAP.md](MODULE_MAP.md), [MODULE_CONTRACT.md](MODULE_CONTRACT.md), and [AGENTS.md](AGENTS.md) for module boundaries and the feature-claim workflow. Claims coordinate cooperating contributors; review and sequential integration are still required.

## License

The manager code is MIT licensed. Driver samples retain their own upstream license notices; see each driver's directory before using or redistributing those samples.
