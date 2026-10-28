# Experimental guest display driver sources

The `drivers/` tree contains two source-only references: Microsoft's Windows Indirect Display Driver sample (MS-PL) and an identity-isolated fork seed of the MIT-licensed MttVDD project. Each directory retains its own upstream license and attribution.

Neither source has been built, signed, installed, or runtime-tested as part of this package. The MttVDD-derived control-pipe implementation retains upstream broad-access code but is dormant in this seed; it must not be enabled or treated as an authenticated interface. The IDD sample connects a monitor on device start and does not implement an activity lease or idle disconnect.

**Never install either candidate on a user's host.** A display driver is not an AI-only monitor boundary. Any future experiment belongs inside a disposable, isolated Windows guest after the broker, guest lifecycle, idle timeout, display departure, and graceful shutdown have been implemented and reviewed.

## Future acceptance checks

1. Build and sign with the intended WDK in the disposable guest only.
2. Confirm display arrival only after an explicitly approved run lease and removal on release, timeout, or controller loss.
3. Confirm host display identities, primary output, foreground, ordinary windows and cursor remain unchanged across guest start, use and shutdown.
4. Verify normal guest shutdown and final VM-Off state; retain failure state if cleanup times out.

Passing Python source tests does not satisfy these driver acceptance checks.
