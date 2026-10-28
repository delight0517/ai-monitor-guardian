# AI Debug Guest Display — MttVDD fork seed

This source snapshot is derived from VirtualDrivers/Virtual-Display-Driver at commit `d7244969b2aa8bb38e76d79505eda217996cefea`, under the included MIT license. The upstream research checkout remains unchanged.

This is a **fail-closed, unbuilt fork seed** for a disposable Windows guest. Its device ID, UMDF group, service image, control-pipe name and configuration namespace are separated from the upstream/host package. Adapter initialization does not create a monitor, and the broad upstream control pipe is not started. It is not ready to install or use.

## Required before guest use

- Add fresh monitor create/arrival and departure behind a run-scoped lease, with 300-second idle expiry and controller-loss cleanup.
- Replace the upstream Everyone-accessible pipe ACL and broad settings commands with a narrow authenticated protocol.
- Validate source against the target WDK, build/sign in the guest, and test start/stop/reconnect there.

`CreateMonitor()` remains in the fork as dormant upstream-derived code, but no lease/controller invokes it. The source also retains upstream pipe implementation code with an Everyone-accessible ACL; it is dormant because neither `DriverEntry` nor adapter initialization starts it. This is not a restricted control protocol and must not be enabled. The candidate has no lease, idle expiry, controller-loss cleanup, or monitor-departure lifecycle yet.

Do not build/install this candidate on the host. Guest-only build/runtime validation is still pending. No host display device, topology, BCD, signing, or VDD Control state is modified by this package.
