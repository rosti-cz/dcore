#!/usr/bin/python3
"""Sign DKMS modules using a build secret; never persist the private key."""
import argparse
import json
import lzma
from pathlib import Path
import subprocess
import tempfile

CERTIFICATE = Path('/usr/share/dcore/secureboot/dcore.crt')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def module_bytes(path):
    data = path.read_bytes()
    if path.suffix == '.xz':
        return lzma.decompress(data)
    if path.suffix == '.zst':
        return run('zstd', '-d', '-c', str(path)).stdout
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, default=Path('/run/secrets/secureboot_key'))
    parser.add_argument('--required', choices=('0', '1'), default='1')
    args = parser.parse_args()
    fingerprint = run('openssl', 'x509', '-in', str(CERTIFICATE), '-noout', '-fingerprint', '-sha256').stdout.decode().strip().split('=', 1)[1]
    metadata = {'version': 1, 'signed': False, 'certificate_sha256': fingerprint,
                'boot_chain': 'Debian shim, systemd-boot and kernel', 'enrollment': 'MOK'}
    if not args.key.is_file():
        if args.required == '1':
            raise ValueError('Secure Boot signing key is required; pass it as a Podman build secret.')
    else:
        private_public = run('openssl', 'pkey', '-in', str(args.key), '-pubout').stdout
        public = run('openssl', 'x509', '-in', str(CERTIFICATE), '-pubkey', '-noout').stdout
        if private_public != public:
            raise ValueError('Signing key does not match the published dcore certificate.')
        kernels = list(Path('/usr/lib/modules').iterdir())
        if len(kernels) != 1:
            raise ValueError('Expected exactly one image kernel.')
        kernel = kernels[0]
        signer = Path('/usr/src') / ('linux-headers-' + kernel.name) / 'scripts/sign-file'
        modules = [p for p in (kernel / 'updates/dkms').rglob('*') if p.is_file() and
                   p.name.endswith(('.ko', '.ko.xz', '.ko.zst'))]
        if not modules:
            raise ValueError('No DKMS modules found to sign.')
        for module in modules:
            with tempfile.TemporaryDirectory() as directory:
                plain = Path(directory) / 'module.ko'
                data = module_bytes(module)
                magic = b'~Module signature appended~\n'
                # Remove DKMS's temporary signature before applying the release key.
                if data.endswith(magic):
                    trailer = data[-len(magic)-12:-len(magic)]
                    signature_size = int.from_bytes(trailer[-4:], 'big')
                    data = data[:-(len(magic) + 12 + signature_size + trailer[3] + trailer[4])]
                plain.write_bytes(data)
                run(str(signer), 'sha256', str(args.key), str(CERTIFICATE), str(plain))
                if module.suffix == '.xz':
                    signed = lzma.compress(plain.read_bytes(), check=lzma.CHECK_CRC32)
                elif module.suffix == '.zst':
                    signed = run('zstd', '-q', '-c', str(plain)).stdout
                else:
                    signed = plain.read_bytes()
                module.write_bytes(signed)
        run('depmod', '-a', kernel.name)
        metadata['signed'] = True
        metadata['modules'] = [str(p.relative_to(kernel)) for p in sorted(modules)]
    Path('/usr/share/dcore/secureboot/status.json').write_text(json.dumps(metadata, indent=2) + '\n')


if __name__ == '__main__':
    main()
