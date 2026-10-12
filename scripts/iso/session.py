"""Recoverable live-boot installer session; checkpoints never contain secrets."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

STEPS = (('load', 'Load bundled image'), ('validate', 'Validate destination'),
         ('install', 'Deploy operating system'), ('configure', 'Configure administrator'),
         ('verify', 'Verify installation'), ('cleanup', 'Release installer resources'))


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class InstallerSession:
    def __init__(self, directory=Path('/run/dcore-installer'),
                 install_dir=Path('/usr/libexec/dcore/install'), medium=Path('/run/live/medium'), config=None):
        self.directory = Path(directory)
        self.install_dir = Path(install_dir)
        self.medium = Path(medium)
        self.config = config if config is not None else json.loads((self.medium / 'dcore/installer.json').read_text())
        self.checks = load_module(Path(__file__).with_name('installer.py'), 'installer_checks')
        self.checker = load_module(self.install_dir / 'lib/check-block-device.py', 'block_check')
        self.passwords = load_module(self.install_dir / 'lib/install-password.py', 'password_tools')
        self.state = {'version': 1, 'settings': None, 'completed': [], 'failed': None, 'error': None}
        self.core_password = self.luks_passphrase = None
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if self.directory.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Installer session directory must be private and owned by the caller.')
        self.lock = os.open(self.directory / 'session.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            raise ValueError('Another installer is using this session.') from None
        state_file = self.directory / 'session.json'
        if state_file.exists():
            self.state = json.loads(state_file.read_text())
            if self.state.get('version') != 1:
                raise ValueError('Unsupported installer checkpoint version.')
            if self.state.get('running'):
                self.state['failed'] = self.state.pop('running')
                self.state['error'] = 'The previous interface stopped during this step. Inspect the log before retrying.'
        self.reconcile()

    def close(self):
        self.core_password = self.luks_passphrase = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def save(self):
        descriptor, temporary = tempfile.mkstemp(prefix='.session-', dir=self.directory)
        try:
            with os.fdopen(descriptor, 'w') as output:
                json.dump(self.state, output)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.directory / 'session.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def layout(self):
        file = self.directory / 'layout.json'
        return json.loads(file.read_text()) if file.exists() else None

    def reconcile(self):
        layout = self.layout()
        if layout and layout.get('deployed') is True:
            for phase in ('load', 'validate', 'install'):
                if phase not in self.state['completed']:
                    self.state['completed'].append(phase)
            for field, phase in (('configured', 'configure'), ('verified', 'verify')):
                if layout.get(field) and phase not in self.state['completed']:
                    self.state['completed'].append(phase)
            if self.state.get('failed') == 'install' or (
                    self.state.get('failed') in ('configure', 'verify') and not layout.get('clean')):
                self.state['failed'] = 'cleanup'
        self.save()

    def check_resources(self):
        self.checks.check_resources(self.config)

    def disks(self):
        # Reuse the exact disk/media checks of the plain interface.
        previous = self.checks.MEDIUM
        self.checks.MEDIUM = self.medium
        try:
            return self.checks.eligible_disks(self.checker, self.checks.media_devices())
        finally:
            self.checks.MEDIUM = previous

    def settings(self, disk, identity, filesystem, encrypt):
        if self.layout():
            raise ValueError('An OS is already deployed. Resume its remaining stages.')
        if filesystem not in ('ext4', 'btrfs', 'xfs'):
            raise ValueError('Choose ext4, Btrfs or XFS.')
        self.state['settings'] = {'device': disk, 'identity': identity, 'filesystem': filesystem,
                                  'encrypt': bool(encrypt)}
        self.state['completed'] = [phase for phase in self.state['completed'] if phase == 'load']
        self.state['failed'] = self.state['error'] = None
        self.save()

    def credentials(self, core_password, luks_passphrase=None):
        self.core_password = self.passwords.validate_password(core_password)
        if self.state['settings']['encrypt']:
            self.luks_passphrase = self.passwords.validate_password(luks_passphrase or b'')

    def payload(self):
        if self.core_password is None:
            raise ValueError('Re-enter the administrator password to resume.')
        if self.state['settings']['encrypt']:
            if self.luks_passphrase is None:
                raise ValueError('Re-enter the disk passphrase to resume.')
            return self.passwords.encode_credentials(self.core_password, self.luks_passphrase)
        return self.core_password

    def command(self, command, payload=None, log=lambda line: None):
        # Stream output, never argv/stdin secrets. The input is bounded to 64 KiB.
        with os.fdopen(os.memfd_create('dcore-installer-input', os.MFD_CLOEXEC), 'w+b') as secret_input:
            if payload is not None:
                secret_input.write(payload)
                secret_input.seek(0)
            process = subprocess.Popen(command, stdin=secret_input, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, errors='replace',
                                       pass_fds=(self.lock,))
            try:
                for line in process.stdout:
                    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line).rstrip()
                    log(clean)
                status = process.wait()
            except BaseException:
                process.terminate()
                process.wait(timeout=30)
                raise
            finally:
                process.stdout.close()
        if status:
            raise ValueError(f'Step returned exit status {status}. See its output in the log.')

    def validate_identity(self, allow_mapping=False):
        settings = self.state['settings']
        device = settings['device']
        expected = settings['identity']
        if not allow_mapping:
            actual = self.checker.check(device)
            if actual != expected:
                raise ValueError('Destination identity or size changed; refusing to continue.')
        else:
            metadata = os.stat(device)
            block = Path(f'/sys/dev/block/{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}').resolve()
            sequence = int((block / 'diskseq').read_text())
            if os.path.realpath(device) != expected['device'] or sequence != expected['diskseq']:
                raise ValueError('Destination identity changed; refusing cleanup.')

    def finish(self, phase, log):
        self.validate_identity(allow_mapping=phase == 'cleanup')
        command = ['podman', 'run', '--rm', '--pull=never', '--network=none', '-i', '--privileged',
                   '--pid=host', '--ipc=host', '--security-opt', 'label=disable',
                   '-v', '/dev:/dev', '-v', '/var/lib/containers:/var/lib/containers',
                   '-v', f'{self.directory}:/run/dcore-install-state',
                   '-v', f'{self.install_dir / "lib"}:/run/dcore-install-lib:ro',
                   self.config['local_image'], 'python3', '/run/dcore-install-lib/finish-install.py',
                   phase, '--state-dir', '/run/dcore-install-state']
        self.command(command, None if phase == 'cleanup' else self.payload(), log)

    def run_phase(self, phase, confirmation=None, log=lambda line: None):
        settings = self.state['settings']
        if settings is None:
            raise ValueError('Select installation settings first.')
        if phase == 'load':
            self.checks.check_resources(self.config)
            exists = subprocess.run(['podman', 'image', 'exists', self.config['local_image']]).returncode == 0
            if exists and '@sha256:' in self.config.get('source_image', ''):
                digest = subprocess.check_output(['podman', 'image', 'inspect', '--format', '{{.Digest}}',
                                                  self.config['local_image']], text=True).strip()
                exists = digest == self.config['source_image'].split('@', 1)[1]
            if not exists:
                self.checks.check_resources(self.config, require_free=True)
                self.command(['podman', 'load', '--input', str(self.medium / 'dcore/image.oci.tar.gz')], log=log)
        elif phase == 'validate':
            if not Path('/sys/firmware/efi').is_dir():
                raise ValueError('Boot the installer in UEFI mode.')
            self.validate_identity()
            if settings['encrypt']:
                self.checks.check_encryption_support()
        elif phase == 'install':
            if self.layout():
                raise ValueError('An OS is already deployed; resume configuration or cleanup.')
            if confirmation != f'ERASE {settings["device"]}':
                raise ValueError('Fresh erase confirmation is required before deployment.')
            self.validate_identity()
            command = [str(self.install_dir / 'block_install'), settings['device'], '--local',
                       '--image', self.config['local_image'], '--target-image', self.config['tracking_image'],
                       '--filesystem', settings['filesystem'], '--core-password-stdin', '--defer-account',
                       '--state-dir', str(self.directory), '--confirm-device', settings['device']]
            if settings['encrypt']:
                command.append('--encrypt')
            self.command(command, self.payload(), log)
        elif phase in ('configure', 'verify', 'cleanup'):
            self.finish(phase, log)
        else:
            raise ValueError('Unknown installer phase.')

    def run(self, confirmation=None, log=lambda line: None, changed=lambda: None):
        self.reconcile()
        if self.state['failed'] == 'cleanup':
            # Release a known leftover mapping before accessing the filesystem.
            try:
                self.finish('cleanup', log)
            except (ValueError, OSError, subprocess.CalledProcessError) as error:
                self.state['error'] = str(error)
                self.save()
                changed()
                return False
            else:
                self.state['failed'] = self.state['error'] = None
                self.save()
        for phase, label in STEPS:
            if phase in self.state['completed']:
                continue
            self.state['running'] = phase
            self.state['failed'] = self.state['error'] = None
            self.save()
            changed()
            log(f'--- {label} ---')
            try:
                self.run_phase(phase, confirmation, log)
            except (ValueError, OSError, subprocess.CalledProcessError) as error:
                self.state['failed'] = phase
                self.state['error'] = str(error)
                self.state.pop('running', None)
                self.reconcile()
                changed()
                return False
            self.state['completed'].append(phase)
            self.state.pop('running', None)
            self.save()
            changed()
        return True


class HostInstallerSession(InstallerSession):
    """Same wizard and checkpoints, with privileged operations isolated by sudo."""
    HOST_MODE = True

    def __init__(self, directory=None, install_dir=Path('/usr/libexec/dcore/install'), backend=None):
        if os.geteuid() == 0:
            raise ValueError('Run djust install directly as an ordinary user, without sudo.')
        self.backend = str(backend or Path(__file__).with_name('host-backend.py'))
        config = self.query_backend('source')
        self.backend_status = self.query_backend('status')
        directory = directory or Path(f'/run/user/{os.geteuid()}/dcore-installer')
        super().__init__(directory=directory, install_dir=install_dir, config=config)

    def backend_command(self, operation):
        return ['/usr/bin/sudo', '--non-interactive', '--', self.backend, operation]

    def query_backend(self, operation, value=None):
        payload = json.dumps(value, ensure_ascii=False).encode('utf-8') if value is not None else None
        result = subprocess.run(self.backend_command(operation), input=payload, capture_output=True)
        if result.returncode:
            message = result.stderr.decode('utf-8', errors='replace').strip()
            raise ValueError(message or 'Privileged installation operation failed.')
        return json.loads(result.stdout) if result.stdout.strip() else None

    def layout(self):
        return self.backend_status['layout']

    def reconcile(self):
        self.backend_status = self.query_backend('status')
        if self.state.get('settings') is None and self.backend_status.get('settings'):
            self.state['settings'] = self.backend_status['settings']
        super().reconcile()

    def check_resources(self):
        # Host mode stages images on the existing filesystem, not a live RAM disk.
        pass

    def disks(self):
        return self.query_backend('disks')

    def settings(self, disk, identity, filesystem, encrypt):
        # The root backend independently validates and records the destination.
        value = {'device': disk, 'identity': identity, 'filesystem': filesystem, 'encrypt': bool(encrypt)}
        self.query_backend('settings', value)
        super().settings(disk, identity, filesystem, encrypt)

    def request(self, phase, confirmation=None, log=lambda line: None):
        request = {}
        if phase in ('install', 'configure', 'verify'):
            self.payload()  # Validate required in-memory credentials before sudo.
            request['core_password'] = self.core_password.decode('utf-8')
            if self.state['settings']['encrypt']:
                request['luks_passphrase'] = self.luks_passphrase.decode('utf-8')
        if phase == 'install':
            request['confirmation'] = confirmation
        self.command(self.backend_command(phase), json.dumps(request, ensure_ascii=False).encode('utf-8'), log)
        self.backend_status = self.query_backend('status')

    def run_phase(self, phase, confirmation=None, log=lambda line: None):
        self.request(phase, confirmation, log)

    def finish(self, phase, log):
        self.request(phase, log=log)
