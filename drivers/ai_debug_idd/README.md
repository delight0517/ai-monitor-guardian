# AI Debug Guest Display IDD (prototype)

Status: source candidate only. It has not been built, signed, installed, or exercised.

This is a guest-only derivative of Microsoft's Windows Indirect Display Driver sample, pinned to commit `bb68ed5e770bce04dc5395ec9258dafab034184d`. Microsoft's original **Microsoft Public License (MS-PL)** notice is retained in `LICENSE`; review that license before redistributing this derivative. The driver reports one virtual display and uses the unique guest-only hardware ID `Root\AiDebugGuestDisplay` and UMDF DeviceGroupId `AiDebugGuestDisplayGroup`. It reports an EDID-less output and labels the adapter as AI Debug Guest Display.

**Install scope: disposable Windows guest VM only. Never install this package on the user's host.** An IDD registers a normal display with Windows; the driver itself cannot restrict the output to one process or keep other guest apps from using it. The host VM boundary is the isolation boundary. Keep only approved QA apps in the guest.

This sample still connects its monitor when the device starts. It does not implement an AI activity lease or five-minute idle disconnect. Those require a guest-side controller/VM lifecycle owner before any runtime use. Do not treat this as satisfying the on-demand or GPU-idle criteria yet.

Build prerequisites are incomplete on the current host: Visual Studio 2022 and MSBuild are present, but WDK driver headers/build targets and Inf2Cat are missing. No disposable Windows guest is available. REA's native decompilation providers are also unavailable. Source provenance and the safe test boundary are recorded in the parent DRIVER_DESIGN.md and TODO.md.

Upstream: https://github.com/microsoft/Windows-driver-samples/tree/bb68ed5e770bce04dc5395ec9258dafab034184d/video/IndirectDisplay
