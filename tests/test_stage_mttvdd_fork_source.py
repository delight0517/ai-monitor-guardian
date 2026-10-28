import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts'/'stage_mttvdd_fork_source.py'
spec=importlib.util.spec_from_file_location('stage_mttvdd_fork_source',SCRIPT)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SOURCE=ROOT/'drivers'/'ai_debug_mttvdd'

class StageMttVddForkTests(unittest.TestCase):
    def test_additive_stage_and_overwrite_refusal(self):
        with tempfile.TemporaryDirectory(prefix='mttvdd-stage-test-') as temp:
            component=Path(temp)
            target=module.stage(SOURCE,component)
            manifest=json.loads((target/'fork-manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['stage'],'fail-closed-not-built-not-for-install')
            self.assertTrue(target.name.endswith(manifest['forkVersion']))
            self.assertFalse(manifest['hostInstallAllowed'])
            for rel,expected in manifest['files'].items():
                self.assertEqual(module.digest(target/rel),expected)
            with self.assertRaises(FileExistsError):
                module.stage(SOURCE,component)

    def test_changed_source_hash_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='mttvdd-source-test-') as temp:
            source=Path(temp)/'source'
            shutil.copytree(SOURCE,source)
            with (source/'MttVDD'/'Driver.cpp').open('a',encoding='utf-8') as stream:
                stream.write('// changed')
            with self.assertRaisesRegex(ValueError,'changed fork source file'):
                module.stage(source,Path(temp))

if __name__=='__main__':
    unittest.main()
