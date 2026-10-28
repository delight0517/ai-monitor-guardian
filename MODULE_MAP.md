# Module map

Package `ai_debug_display` version 0.7.0, API 1.

| Feature | Owned boundary | Apply boundary | Evidence / limit |
|---|---|---|---|
| Display policy | `modules/display_policy.py`, policy facade and contracts | Module host can reload policy only while no session or policy call is active | Local policy tests; no host display adapter is enabled |
| Session lifecycle | `models.py`, `service.py`, activity lease | End the exact session before replacing core imports | Fake adapter tests |
| Private desktop | `private_desktop.py` | Close the owned process/job/desktop before replacement | Windows disposable-process tests; focus isolation only |
| Reconnect recovery | `reconnect_recovery.py`, `placement_journal.py` | Persist validated state, wait for callbacks to finish, discard old plans | Policy/persistence tests; no general watcher or hardware cycle |
| Guest lifecycle | `guest_session.py` | Stop the lease and backend before replacement | Fake backend tests; no production Hyper-V backend |
| Broker protocol | `broker_protocol.py` | Roll out broker/client versions together | Parser and synthetic interop tests; no server/authentication |
| Evolution host | `evolution.py`, `module-manifest.json` | Candidate hash, API, version and state guards; one module per apply | In-process module host tests; no installed service |
| Feature claims | `feature_claims.py`, `feature-ownership.json`, `scripts/hold_feature_claim.py` | One writer per feature; exclusive integration claim | Cross-process coordination tests; cooperating writers only |
| Driver samples | `drivers/` | Build/sign/install only in a disposable guest after review | Source-only; unbuilt and unvalidated |

## Composition and state

The Python package is the source library. There is no always-running UI or service. The optional module host loads trusted Python code in-process; it is not a sandbox. Persisted user state must stay outside replaceable module code. Changes to package imports, manifests, native adapters or drivers require their documented process/build boundary.

`FeatureClaimCoordinator` permits different declared feature sets to be edited concurrently by contributors that use its lock protocol. Same-feature writers are exclusive, and composition/release work is serialized through `integration`. This is coordination, not an OS-enforced write barrier, branch isolation, or automatic merge.

## Runtime status

The package does not activate a host-attached virtual display, own a production VM, provide an authenticated broker, capture guest frames, or guarantee user-window recovery. A host VDD can become an ordinary Windows display and can affect the user's display topology. The default host adapter is inert.
