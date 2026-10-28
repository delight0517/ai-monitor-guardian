"""Stage the selected MttVDD fork seed as a read-only Only Chat component."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def stage(source: Path, component_root: Path) -> Path:
    source=source.resolve(strict=True)
    component_root=component_root.resolve(strict=True)
    manifest=json.loads((source/'fork-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('stage')!='fail-closed-not-built-not-for-install' or manifest.get('hostInstallAllowed') is not False:
        raise ValueError('Refusing to stage a candidate not explicitly marked source-only/guest-only.')
    if manifest.get('license')!='MIT' or manifest.get('upstreamCommit')!='d7244969b2aa8bb38e76d79505eda217996cefea':
        raise ValueError('Unexpected upstream provenance/license.')
    files=manifest.get('files')
    if not isinstance(files,dict) or not files or len(files)>64:
        raise ValueError('Invalid fork file manifest.')
    version=manifest.get('forkVersion')
    if not isinstance(version,str) or not version.replace('.','').replace('-','').isalnum():
        raise ValueError('Invalid fork version for component staging.')
    target=component_root/f'mttvdd-fork-source-{version}'
    if target.exists():
        raise FileExistsError(f'Refusing to overwrite existing component: {target}')
    for rel,expected in files.items():
        src=(source/rel).resolve(strict=True)
        if not src.is_file() or source not in src.parents or digest(src)!=expected:
            raise ValueError(f'Invalid or changed fork source file: {rel}')
    temp=Path(tempfile.mkdtemp(prefix='.mttvdd-fork-source-',dir=component_root))
    try:
        for rel in files:
            dst=temp/rel
            dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/rel,dst)
        shutil.copyfile(source/'fork-manifest.json',temp/'fork-manifest.json')
        copied=json.loads((temp/'fork-manifest.json').read_text(encoding='utf-8'))
        if any(digest(temp/rel)!=expected for rel,expected in copied['files'].items()):
            raise IOError('Staged MttVDD fork hash verification failed.')
        os.replace(temp,target)
        return target
    finally:
        if temp.exists(): shutil.rmtree(temp)

def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--component-root',type=Path,required=True)
    args=parser.parse_args()
    target=stage(args.source,args.component_root)
    print(f'Staged hash-verified source-only MttVDD fork: {target}')
    print('Not built, not signed, not installable, no lease behavior, guest only.')
    return 0
if __name__=='__main__': raise SystemExit(main())
