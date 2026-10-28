# Module contract

This contract describes the source library, not an installed desktop product.

## Stable boundaries

- `models.py` contains versioned, content-free session and output records.
- `policy.py` and `modules/display_policy.py` decide whether a requested QA display operation is allowed. Unknown topology fails closed.
- `service.py` owns one bounded session and delegates all platform effects to an injected adapter. The default adapter does nothing.
- `private_desktop.py` owns only its created process/job/private desktop. It does not switch the interactive input desktop and is not a security sandbox.
- `PrivateDesktopSession.wait_for_owned_window()` is the readiness gate: it returns only top-level HWND/PID pairs enumerated on the fresh desktop whose process belongs to the run's Job. An exited launcher or timeout fails closed and cleans up the exact Job/desktop, preventing the consumer from treating an app that forwarded to an existing user instance as isolated QA.
- `PrivateDesktopSession.capture_owned_window(hwnd)` revalidates the HWND and calls `PrintWindow(PW_RENDERFULLCONTENT)` in a short-lived helper assigned to a kill-on-close Job on the same private desktop. It returns an in-memory `BGRX_BOTTOM_UP` buffer capped at 64 MiB. A 3-second deadline terminates only the helper and leaves the QA app running. It does not switch desktops, write a screenshot file, or fall back to a visible-screen capture. A synthetic GDI fixture returned its expected pixels; accelerated/protected/elevated and app-specific windows may fail or return blank and need separate verification.
- The process still runs as the logged-in Windows user. A private desktop isolates focus/window placement, not files, credentials, network, app profiles, or untrusted code.
- `reconnect_recovery.py` and `placement_journal.py` validate explicitly selected placements. They do not enumerate or move arbitrary user windows by themselves.
- `guest_session.py` defines a fixed-guest, run-scoped lifecycle around an injected backend. No production backend is included.
- `broker_protocol.py` parses a bounded API-v1 message with a small operation allowlist. Parsing does not authenticate a peer or create a secure named pipe.
- `evolution.py` stages and applies one trusted source module at a time with API/version/hash checks, lifecycle callbacks and state recovery.
- `feature_claims.py` coordinates contributors over declared file sets. It does not enforce access control outside cooperating writers.

## Replacement rules

Feature modules must expose explicit initialization/disposal and state snapshot/restore behavior. A candidate must match the expected API and canonical source, advance its version, and pass its focused checks. Apply is rejected while owned work or callbacks are active. Failed candidates restore the prior module state. Integration remains sequential; competing branches are never merged automatically.

UI, domain behavior, persistence, provider adapters and native/platform adapters belong in separate consumer-owned modules. User records, drafts, credentials and settings must remain outside replaceable module binaries. Native drivers and compiled dependencies need their own build/sign/install process and cannot be called hot-reloadable.

## Threat boundary

Trusted Python module code executes with its process permissions. Feature claims are file-edit coordination only. Private desktops isolate focus, not filesystem, credentials, account or network access. Real monitor topology remains controlled by Windows and installed display drivers; this package does not claim otherwise.
