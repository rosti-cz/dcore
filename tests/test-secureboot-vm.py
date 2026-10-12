#!/usr/bin/python3
"""Install and boot a disposable signed dcore disk with Secure Boot and MOK trust.

Requires root on an amd64 host: Podman, QEMU, OVMF, loop tools, SSH and
virt-fw-vars (python3-virt-firmware). Never uses physical destination disks.
"""
import argparse
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import shutil
import shlex
import socket
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', required=True)
parser.add_argument('--filesystem', choices=('ext4', 'btrfs', 'xfs'), default='ext4')
parser.add_argument('--encrypt', action='store_true')
parser.add_argument('--vars-tool', default='virt-fw-vars')
parser.add_argument('--ovmf-code')
parser.add_argument('--ovmf-vars')
args = parser.parse_args()
if os.geteuid() != 0:
    raise SystemExit('Run this disposable VM test as root on the host.')

def firmware(explicit, candidates):
    if explicit:
        return explicit
    return str(next(Path(p) for p in candidates if Path(p).is_file()))

code = firmware(args.ovmf_code, ['/usr/share/OVMF/OVMF_CODE_4M.secboot.fd', '/usr/share/OVMF/OVMF_CODE.secboot.fd'])
variables = firmware(args.ovmf_vars, ['/usr/share/OVMF/OVMF_VARS_4M.ms.fd', '/usr/share/OVMF/OVMF_VARS.secboot.fd'])
lock = os.open('/run/lock/dcore-loop-tests.lock', os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX)
(ROOT/'dist').mkdir(exist_ok=True)
work = Path(tempfile.mkdtemp(prefix='secureboot-vm-', dir=ROOT/'dist'))
print(f'VM test logs: {work}', flush=True)
raw, nvram = work/'disk.raw', work/'vars.fd'
with raw.open('wb') as output: output.truncate(10 * 1024**3)
shutil.copy(variables, nvram)
core, luks = b'dcore-test-core-password', b'dcore-test-disk-password'
askpass = work/'askpass'
askpass.write_text('#!/bin/sh\nprintf "%s" "$DCORE_VM_TEST_PASSWORD"\n')
askpass.chmod(0o700)
environment = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE='force',
                   DISPLAY=':0', DCORE_VM_TEST_PASSWORD=core.decode())
port = socket.socket()
port.bind(('127.0.0.1', 0))
ssh_port = port.getsockname()[1]
port.close()
ssh = ['ssh', '-F', '/dev/null', '-p', str(ssh_port), '-o', 'StrictHostKeyChecking=no',
       '-o', f'UserKnownHostsFile={work}/known_hosts', '-o', 'PreferredAuthentications=password',
       '-o', 'PubkeyAuthentication=no', '-o', 'NumberOfPasswordPrompts=1', '-o', 'ConnectTimeout=3', 'core@127.0.0.1']
loop = vm = serial = None
try:
    loop = subprocess.check_output(['losetup', '--find', '--show', '--partscan', str(raw)], text=True).strip()
    spec = importlib.util.spec_from_file_location('password_tools', ROOT/'scripts/lib/install-password.py')
    passwords = importlib.util.module_from_spec(spec); spec.loader.exec_module(passwords)
    payload = passwords.encode_credentials(core, luks) if args.encrypt else core
    command = [str(ROOT/'scripts/block_install'), loop, '--local', '--image', args.image,
               '--target-image', 'ghcr.io/rosti-cz/dcore:trixie', '--filesystem', args.filesystem,
               '--core-password-stdin', '--confirm-device', loop]
    if args.encrypt: command.append('--encrypt')
    with (work/'install.log').open('wb') as output:
        try:
            subprocess.run(command, input=payload, stdout=output, stderr=subprocess.STDOUT, check=True)
        except subprocess.CalledProcessError:
            print((work/'install.log').read_text(errors='replace')[-6000:], flush=True)
            raise
    fixture = '''set -eu
mkdir /work/esp
mount "$ESP" /work/esp
trap 'umount /work/esp' EXIT
python3 - <<'SERIAL'
from pathlib import Path
for entry in Path('/work/esp/loader/entries').glob('*.conf'):
    lines=entry.read_text().splitlines()
    entry.write_text('\\n'.join(line+' console=ttyS0,115200n8' if line.startswith('options ') else line for line in lines)+'\\n')
SERIAL
'''
    subprocess.run(['podman', 'run', '--rm', '--network=none', '--privileged', '-v', '/dev:/dev',
        '-v', f'{work}:/work', '-e', f'ESP={loop}p2', args.image, 'bash', '-c', fixture], check=True)
    subprocess.run(['losetup', '--detach', loop], check=True); loop = None
    certificate = work/'dcore.crt'
    certificate.write_bytes(subprocess.check_output(['podman', 'run', '--rm', args.image,
                                                     'cat', '/usr/share/dcore/secureboot/dcore.crt']))

    def guest(command, check=True):
        result = subprocess.run(ssh+[command], env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        if check and result.returncode:
            print(result.stdout + result.stderr, flush=True)
            result.check_returncode()
        return result

    for enrolled in (False, True):
        label = 'enrolled' if enrolled else 'unenrolled'
        if enrolled:
            subprocess.run([args.vars_tool, '--input', str(nvram), '--output', str(work/'enrolled.fd'),
                '--add-mok', '605dab50-e046-4300-abb6-3dd810dd8b23', str(certificate)], check=True)
            os.replace(work/'enrolled.fd', nvram)
        serial_socket, serial_log = work/'serial.sock', work/(label+'-serial.log')
        serial_socket.unlink(missing_ok=True)
        accelerator = ['-enable-kvm', '-cpu', 'host'] if Path('/dev/kvm').exists() else ['-accel', 'tcg', '-cpu', 'max']
        qemu = ['qemu-system-x86_64', *accelerator, '-machine', 'q35,smm=on',
            '-global', 'driver=cfi.pflash01,property=secure,value=on', '-m', '3072', '-smp', '2',
            '-drive', f'if=pflash,format=raw,readonly=on,file={code}',
            '-drive', f'if=pflash,format=raw,file={nvram}', '-drive', f'if=virtio,format=raw,file={raw}',
            '-netdev', f'user,id=n,hostfwd=tcp:127.0.0.1:{ssh_port}-:22', '-device', 'virtio-net-pci,netdev=n',
            '-display', 'none', '-monitor', 'none', '-chardev',
            f'socket,id=serial,path={serial_socket},server=on,wait=off,logfile={serial_log}', '-serial', 'chardev:serial']
        with (work/(label+'-qemu.log')).open('wb') as output:
            vm = subprocess.Popen(qemu, stdout=output, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 240
            while not serial_socket.exists():
                if vm.poll() is not None: raise RuntimeError('QEMU failed; inspect '+str(work))
                if time.monotonic() > deadline: raise TimeoutError('QEMU serial socket')
                time.sleep(.1)
            serial = socket.socket(socket.AF_UNIX); serial.connect(str(serial_socket))
            def drain(connection):
                try:
                    while connection.recv(65536): pass
                except OSError: pass
            threading.Thread(target=drain, args=(serial,), daemon=True).start()
            if args.encrypt:
                while not re.search(r'(?i)(please enter passphrase|passphrase for disk)', serial_log.read_text(errors='replace')):
                    if time.monotonic() > deadline: raise TimeoutError('LUKS unlock prompt')
                    time.sleep(.5)
                serial.sendall(luks+b'\n')
            while guest('id -un', check=False).returncode:
                if vm.poll() is not None or time.monotonic() > deadline:
                    raise TimeoutError('Secure Boot guest login; inspect '+str(work))
                time.sleep(2)
            checks = guest('sudo mokutil --sb-state; sudo cat /sys/kernel/security/lockdown; findmnt -n -o FSTYPE /sysroot; sudo bootctl update')
            (work/(label+'-checks.log')).write_text(checks.stdout + checks.stderr)
            assert 'SecureBoot enabled' in checks.stdout and '[integrity]' in checks.stdout, checks.stdout
            assert args.filesystem in checks.stdout.splitlines(), checks.stdout
            load = guest('sudo modprobe zfs', check=False)
            if enrolled:
                assert load.returncode == 0, load.stderr
                assert 'dcore Secure Boot Signing' in guest('modinfo -F signer zfs').stdout
                # Reject a signature-stripped SPL module after unloading ZFS.
                guest('sudo systemctl stop zfs-zed.service; sudo modprobe -r zfs; test ! -d /sys/module/spl')
                program = "import lzma; from pathlib import Path; p=next(Path('/usr/lib/modules').glob('*/updates/dkms/spl.ko.xz')); d=lzma.decompress(p.read_bytes()); m=b'~Module signature appended~\\n'; e=len(d)-len(m)-12; n=int.from_bytes(d[e+8:e+12],'big'); Path('/var/tmp/unsigned-spl.ko').write_bytes(d[:e-n])"
                guest('sudo python3 -c ' + shlex.quote(program))
                invalid = guest('sudo insmod /var/tmp/unsigned-spl.ko', check=False)
                assert invalid.returncode and ('Key was rejected' in invalid.stderr or 'Required key' in invalid.stderr), invalid.stderr
                guest('sudo modprobe zfs')
            else:
                assert load.returncode and ('Key was rejected' in load.stderr or 'Required key' in load.stderr), load.stderr
            guest('sudo systemctl poweroff', check=False)
            vm.wait(timeout=30); vm = None
            serial.close(); serial = None
            print(f'PASS {args.filesystem} {"LUKS2" if args.encrypt else "plain"} {label}: Secure Boot, lockdown, login, shim-preserving update and module enforcement', flush=True)
    raw.unlink()
finally:
    if vm is not None:
        vm.terminate(); vm.wait(timeout=30)
    if serial is not None: serial.close()
    if loop is not None: subprocess.run(['losetup', '--detach', loop], check=True)
    os.close(lock)
