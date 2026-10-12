#!/usr/bin/python3
"""Privileged, noninteractive operations for djust's ordinary-user installer."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
INSTALL = Path('/usr/libexec/dcore/install')
STATE_ROOT = Path('/run/dcore-install-host')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def source_from_status(status):
    image = (status.get('status', {}).get('booted') or {}).get('image')
    if not image:
        raise ValueError('Run djust install on an installed bootc host.')
    reference = image['image']
    transport = reference.get('transport', 'registry') if isinstance(reference, dict) else 'registry'
    reference = reference['image'] if isinstance(reference, dict) else reference
    digest = image.get('imageDigest', '')
    if transport != 'registry' or not isinstance(reference, str) or not reference or reference.startswith('-'):
        raise ValueError('The booted image must have an OCI registry reference.')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Cannot identify the exact booted image digest; installation refused.')
    repository = reference.split('@')[0]
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    pinned = repository + '@' + digest
    return {'mode': 'host', 'source_image': pinned, 'local_image': pinned,
            'tracking_image': reference, 'release': 'current booted image', 'debian_release': 'dcore'}


def write_json(directory, filename, value):
    fd, temporary = tempfile.mkstemp(prefix='.host-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory/filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Installer backend directory must be private and owned by root.')


class Backend:
    def __init__(self, uid, root=STATE_ROOT):
        private_directory(root)
        self.directory = root / str(uid)
        private_directory(self.directory)
        self.checks = module('installer_checks', Path(__file__).with_name('installer.py'))
        self.checker = module('block_check', INSTALL/'lib/check-block-device.py')
        self.passwords = module('password_tools', INSTALL/'lib/install-password.py')

    def read(self, filename):
        path = self.directory/filename
        return json.loads(path.read_text()) if path.exists() else None

    def source(self):
        config = self.read('source.json')
        if not config:
            status = json.loads(subprocess.check_output(['bootc', 'status', '--format=json'], text=True))
            config = source_from_status(status)
            write_json(self.directory, 'source.json', config)
        return config

    def identity(self, settings, cleanup=False):
        if not cleanup:
            if self.checker.check(settings['device']) != settings['identity']:
                raise ValueError('Destination identity or size changed; refusing to continue.')
        else:
            metadata = os.stat(settings['device'])
            block = Path(f'/sys/dev/block/{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}').resolve()
            expected = settings['identity']
            if (os.path.realpath(settings['device']) != expected['device'] or
                f'{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}' != expected['major_minor'] or
                int((block/'diskseq').read_text()) != expected['diskseq']):
                raise ValueError('Destination identity changed; refusing cleanup.')

    def settings(self, value):
        if self.read('layout.json'):
            raise ValueError('An OS is already deployed. Resume its remaining stages.')
        if not isinstance(value, dict) or set(value) != {'device', 'identity', 'filesystem', 'encrypt'}:
            raise ValueError('Invalid installation settings.')
        if not isinstance(value['device'], str) or not isinstance(value['identity'], dict):
            raise ValueError('Invalid destination identity.')
        if value['filesystem'] not in ('ext4', 'btrfs', 'xfs') or type(value['encrypt']) is not bool:
            raise ValueError('Invalid filesystem or encryption selection.')
        self.identity(value)
        write_json(self.directory, 'settings.json', value)

    def cached_image(self, config):
        expected = config['source_image'].split('@', 1)[1]
        names = [config['source_image'], config['tracking_image']]
        listing = subprocess.run(['podman', 'images', '--no-trunc', '--format', '{{.ID}}'],
                                 text=True, capture_output=True, check=True)
        names.extend(listing.stdout.splitlines())
        for name in dict.fromkeys(names):
            result = subprocess.run(['podman', 'image', 'inspect', name], text=True, capture_output=True)
            if result.returncode:
                continue
            for image in json.loads(result.stdout):
                if image.get('Digest') == expected:
                    identifier = image.get('Id', image.get('ID', ''))
                    if not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', identifier):
                        raise ValueError('Cached image has an invalid immutable image ID.')
                    return identifier
        return None

    def load(self):
        config = self.source()
        identifier = self.cached_image(config)
        if identifier is None:
            if shutil.disk_usage('/var/lib/containers').free < 10 * 1024**3:
                raise ValueError('Image staging needs at least 10 GiB free on the host filesystem at /var/lib/containers. The destination disk has not been changed.')
            run('podman', 'pull', config['source_image'])
            identifier = self.cached_image(config)
        if identifier is None:
            raise ValueError('Cached image digest does not match the booted image.')
        # Tags and repo aliases can move or be absent after loading an archive.
        # The immutable configuration ID keeps subsequent stages on this image.
        config['local_image'] = identifier
        write_json(self.directory, 'source.json', config)

    def phase(self, phase, request):
        settings = self.read('settings.json')
        if not settings:
            raise ValueError('Choose installation settings first.')
        config = self.source()
        if phase == 'load':
            self.load()
            return
        if phase == 'validate':
            if not Path('/sys/firmware/efi').is_dir():
                raise ValueError('This installer requires an amd64 UEFI host.')
            self.identity(settings)
            if settings['encrypt']:
                self.checks.check_encryption_support()
            return
        if phase == 'install':
            if self.read('layout.json'):
                raise ValueError('An OS is already deployed; refusing another erase.')
            if request.get('confirmation') != 'ERASE ' + settings['device']:
                raise ValueError('Fresh erase confirmation is required before deployment.')
            self.identity(settings)
            image_size = int(subprocess.check_output(['podman', 'image', 'inspect', '--format', '{{.Size}}', config['local_image']], text=True))
            if shutil.disk_usage('/var/lib/containers').free < image_size + 1024**3:
                raise ValueError('Insufficient free host disk space for installation staging; destination unchanged.')
            args = [str(INSTALL/'block_install'), settings['device'], '--local',
                    '--image', config['local_image'], '--target-image', config['tracking_image'],
                    '--filesystem', settings['filesystem'], '--core-password-stdin', '--defer-account',
                    '--state-dir', str(self.directory), '--confirm-device', settings['device']]
            if settings['encrypt']:
                args.append('--encrypt')
            run(*args, input=self.payload(settings, request))
            return
        if phase not in ('configure', 'verify', 'cleanup'):
            raise ValueError('Unknown installation operation.')
        self.identity(settings, cleanup=phase == 'cleanup')
        args = ['podman', 'run', '--rm', '--pull=never', '--network=none', '-i', '--privileged',
                '--pid=host', '--ipc=host', '--security-opt', 'label=disable',
                '-v', '/dev:/dev', '-v', '/var/lib/containers:/var/lib/containers',
                '-v', f'{self.directory}:/run/dcore-install-state',
                '-v', f'{INSTALL/"lib"}:/run/dcore-install-lib:ro', config['local_image'],
                'python3', '/run/dcore-install-lib/finish-install.py', phase,
                '--state-dir', '/run/dcore-install-state']
        run(*args, input=None if phase == 'cleanup' else self.payload(settings, request))

    def payload(self, settings, request):
        try:
            password = self.passwords.validate_password(request['core_password'].encode('utf-8'))
            if settings['encrypt']:
                return self.passwords.encode_credentials(password, request['luks_passphrase'].encode('utf-8'))
            return password
        except (KeyError, AttributeError):
            raise ValueError('Re-enter the required installation credentials.') from None


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('source', 'disks', 'status', 'settings', 'load',
                                            'validate', 'install', 'configure', 'verify', 'cleanup'))
    args = parser.parse_args()
    uid = os.environ.get('SUDO_UID', '')
    if os.geteuid() != 0 or not uid.isdecimal() or int(uid) <= 0:
        raise ValueError('Launch the installer directly as an ordinary user through djust install.')
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    backend = Backend(int(uid))
    if args.operation in ('source', 'status', 'disks'):
        if args.operation == 'source':
            result = backend.source()
        elif args.operation == 'status':
            result = {'layout': backend.read('layout.json'), 'settings': backend.read('settings.json')}
        else:
            result = backend.checks.eligible_disks(backend.checker, set())
        print(json.dumps(result))
        return
    fd = os.open(STATE_ROOT/'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another disk installation operation is running; retry later.') from None
        raw = sys.stdin.buffer.read(65537)
        try:
            if len(raw) > 65536:
                raise ValueError()
            request = json.loads(raw or b'{}')
            if not isinstance(request, dict):
                raise ValueError()
        except (ValueError, RecursionError):
            raise ValueError('Invalid installer request.') from None
        if args.operation == 'settings':
            backend.settings(request)
        else:
            backend.phase(args.operation, request)
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f'Installation operation failed: {error}', file=sys.stderr)
        sys.exit(1)
