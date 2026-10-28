import json
import hashlib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORK = ROOT / 'drivers' / 'ai_debug_mttvdd'
DRIVER = FORK / 'MttVDD'

class MttVddForkSeedTests(unittest.TestCase):
    def test_identity_isolated_from_upstream(self):
        manifest = json.loads((FORK / 'fork-manifest.json').read_text(encoding='utf-8'))
        inf = (DRIVER / 'AiDebugGuestDisplay.inf').read_text(encoding='utf-8')
        cpp = (DRIVER / 'Driver.cpp').read_text(encoding='utf-8')
        project = (DRIVER / 'AiDebugGuestDisplay.vcxproj').read_text(encoding='utf-8')
        self.assertEqual(manifest['upstreamCommit'], 'd7244969b2aa8bb38e76d79505eda217996cefea')
        self.assertEqual(manifest['license'], 'MIT')
        self.assertEqual(manifest['installScope'], 'disposable-windows-guest-only')
        self.assertFalse(manifest['hostInstallAllowed'])
        for relative, expected in manifest['files'].items():
            content = (FORK / relative).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), expected, relative)
        self.assertIn(r'Root\AiDebugGuestDisplay', inf)
        self.assertNotIn(r'Root\MttVDD', inf)
        self.assertIn('AiDebugGuestDisplayGroup', inf)
        self.assertIn('AiDebugGuestDisplay_Install', inf)
        self.assertIn('AiDebugGuestDisplay.dll', inf)
        self.assertIn('AiDebugGuestDisplayPipe', cpp)
        self.assertNotIn(r'SOFTWARE\MikeTheTech\VirtualDisplayDriver', cpp)
        self.assertIn('<RootNamespace>AiDebugGuestDisplay</RootNamespace>', project)
        self.assertIn(r'SOFTWARE\\AiDebugDisplay', cpp)
        self.assertIn(r'C:\\ProgramData\\AiDebugDisplay', cpp)

    def test_seed_is_not_misrepresented_as_usable(self):
        manifest = json.loads((FORK / 'fork-manifest.json').read_text(encoding='utf-8'))
        cpp = (DRIVER / 'Driver.cpp').read_text(encoding='utf-8')
        self.assertEqual(manifest['stage'], 'fail-closed-not-built-not-for-install')
        self.assertFalse(manifest['leaseImplemented'])
        self.assertTrue(manifest['defaultMonitorDisabled'])
        self.assertFalse(manifest['controlPipeRestricted'])
        self.assertFalse(manifest['controlPipeEnabledByDefault'])
        self.assertIn('D:(A;;GA;;;WD)', cpp)
        finish_init = cpp.split('void IndirectDeviceContext::FinishInit()', 1)[1].split('void IndirectDeviceContext::CreateMonitor', 1)[0]
        self.assertNotIn('CreateMonitor(', finish_init)
        driver_entry = cpp.split('NTSTATUS DriverEntry', 1)[1].split('vector<string> split', 1)[0]
        self.assertNotIn('StartNamedPipeServer();', driver_entry)
        self.assertIn('StartNamedPipeServer()', cpp)

if __name__ == '__main__':
    unittest.main()
