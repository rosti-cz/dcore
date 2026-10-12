#!/usr/bin/python3
"""Reproduce OCI import ENOSPC and retry on the dedicated RAM mount."""
import importlib.util
import json
import tarfile
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('iso_checks', ROOT / 'scripts/iso/installer.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ImageStaging(unittest.TestCase):
    def test_small_overlay_failure_and_ram_staging_retry(self):
        if os.geteuid() != 0 or not shutil.which('podman'):
            self.skipTest('run with sudo and Podman; uses private stores and tmpfs mounts')
        work = Path(tempfile.mkdtemp(prefix='dcore-image-staging-'))
        mounts = []
        original_tmpdir = os.environ.get('TMPDIR')
        try:
            source = work / 'source'
            source.mkdir()
            (source / 'Containerfile').write_text('FROM scratch\nCOPY payload /payload\n')
            with (source / 'payload').open('wb') as output:
                for _ in range(32):
                    output.write(os.urandom(1024**2))
            source_podman = ['podman', '--root', str(work/'source-store'), '--runroot', str(work/'source-run'), '--storage-driver', 'vfs']
            def run(args, **kwargs):
                return subprocess.run(args, text=True, capture_output=True, **kwargs)
            build = run(source_podman + ['build', '--network=none', '--pull=never', '-t', 'localhost/dcore-staging:fixture', str(source)])
            self.assertEqual(build.returncode, 0, build.stderr)
            archive = work / 'image.oci.tar'
            saved = run(source_podman + ['save', '--format=oci-archive', '--uncompressed', '-o', str(archive), 'localhost/dcore-staging:fixture'])
            self.assertEqual(saved.returncode, 0, saved.stderr)
            expected = run(source_podman + ['image', 'inspect', '--format', '{{.Digest}}', 'localhost/dcore-staging:fixture']).stdout.strip()
            with tarfile.open(archive) as contents:
                index = json.load(contents.extractfile('index.json'))
            self.assertIn(expected, [item['digest'] for item in index['manifests']])
            small = work/'small-temp'
            staging = work/'staging'
            for path, size in ((small, '1M'), (staging, '2G')):
                path.mkdir()
                subprocess.run(['mount', '-t', 'tmpfs', '-o', f'size={size},mode=0700', 'tmpfs', str(path)], check=True)
                mounts.append(path)
            podman = ['podman', '--root', str(staging/'storage'), '--runroot', str(work/'destination-run'), '--storage-driver', 'vfs']
            os.environ['TMPDIR'] = str(small)
            failed = run(podman + ['load', '--input', str(archive)])
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('no space left on device', failed.stderr)
            self.assertIn(str(small), failed.stderr)
            # This tiny fixture needs less memory than the full release image.
            installer.check_resources({'minimum_ram_bytes': 2*installer.GIB, 'image_size_bytes': 32*1024**2},
                                      require_free=True, staging=staging)
            self.assertEqual(os.environ['TMPDIR'], str(staging/'image-tmp'))
            loaded = run(podman + ['load', '--input', str(archive)])
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertEqual(run(podman + ['image', 'exists', 'localhost/dcore-staging:fixture']).returncode, 0)
            self.assertFalse(list((staging/'image-tmp').iterdir()), 'import temporary files were not released')
        finally:
            if original_tmpdir is None:
                os.environ.pop('TMPDIR', None)
            else:
                os.environ['TMPDIR'] = original_tmpdir
            # Never remove mounted content recursively if an unmount fails.
            for path in reversed(mounts):
                subprocess.run(['umount', str(path)], check=True)
            shutil.rmtree(work)


if __name__ == '__main__':
    unittest.main(verbosity=2)
