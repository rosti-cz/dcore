#!/usr/bin/python3
"""Install a local dcore image and test encrypted ZFS storage across QEMU boots.

Requires root, Podman, QEMU, OVMF, loop tools and SSH. All destination disks
are newly created files. Secure Boot enforcement is covered separately by
test-secureboot-vm.py. Logs and disposable disks remain in dist/ for inspection.
"""
import argparse, fcntl, os, pathlib, subprocess, socket, time, shutil, shlex, tempfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', required=True, help='already built local OCI image')
parser.add_argument('--ovmf-code')
parser.add_argument('--ovmf-vars')
args=parser.parse_args()
if os.geteuid()!=0:
    raise SystemExit('Run as root on the development host.')
lock=os.open('/run/lock/dcore-loop-tests.lock',os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(lock,fcntl.LOCK_EX)
(ROOT/'dist').mkdir(exist_ok=True)
work=pathlib.Path(tempfile.mkdtemp(prefix='zfs-boot-qemu-',dir=ROOT/'dist'))
print('VM test logs: '+str(work),flush=True)
image=args.image
if bool(args.ovmf_code)!=bool(args.ovmf_vars):
    raise SystemExit('Supply both --ovmf-code and --ovmf-vars.')
if args.ovmf_code:
    firmware_code,firmware_vars=args.ovmf_code,args.ovmf_vars
else:
    pairs=[('/usr/share/OVMF/OVMF_CODE_4M.fd','/usr/share/OVMF/OVMF_VARS_4M.fd'),
           ('/usr/share/OVMF/OVMF_CODE.fd','/usr/share/OVMF/OVMF_VARS.fd')]
    firmware_code,firmware_vars=next((code,var) for code,var in pairs
                                   if pathlib.Path(code).exists() and pathlib.Path(var).exists())
vm=None; loop=None; serial=None

def run(args, **kw):
    return subprocess.run(args,check=True,**kw)

def log(name, command, check=True):
    result=subprocess.run(command,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (work/(name+'.log')).write_text(result.stdout)
    if check and result.returncode:
        raise RuntimeError(name+': '+result.stdout[-6000:])
    return result

key=work/'id_ed25519'
if not key.exists(): run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)])
port=socket.socket();port.bind(('127.0.0.1',0));ssh_port=port.getsockname()[1];port.close()
ssh=['ssh','-F','/dev/null','-p',str(ssh_port),'-i',str(key),'-o','BatchMode=yes','-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile='+str(work/'known_hosts'),'-o','ConnectTimeout=3','root@127.0.0.1']
def guest(name, command, check=True):
    return log(name,ssh+['bash -eu -c '+shlex.quote(command)],check)
def boot(label,reverse=False):
    global vm, serial
    serial=(work/(label+'-serial.log')).open('wb')
    disks=['b','a','c'] if reverse else ['a','b','c']
    accelerator=['-enable-kvm','-cpu','host'] if pathlib.Path('/dev/kvm').exists() else ['-accel','tcg','-cpu','max']
    command=['qemu-system-x86_64',*accelerator,'-machine','q35','-m','4096','-smp','4','-drive','if=pflash,format=raw,readonly=on,file='+firmware_code,'-drive','if=pflash,format=raw,file='+str(work/'vars.fd'),'-drive','if=virtio,format=raw,file='+str(work/'system.raw')]
    for name in disks: command += ['-drive',f'if=none,id=data{name},format=raw,file={work}/{name}.raw','-device',f'virtio-blk-pci,drive=data{name},serial=zfs-test-{name}']
    command += ['-netdev',f'user,id=n,hostfwd=tcp:127.0.0.1:{ssh_port}-:22','-device','virtio-net-pci,netdev=n','-display','none','-serial','stdio','-monitor','none']
    vm=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=serial,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+300
    while True:
        result=subprocess.run(ssh+['true'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode==0: break
        if vm.poll() is not None or time.monotonic()>deadline: raise RuntimeError('VM did not boot: '+label)
        time.sleep(2)
    print('BOOTED '+label,flush=True)
def poweroff():
    global vm,serial
    # The dummy PID 1 has no shutdown handler. Stop it explicitly; autostart
    # remains enabled so the next boot still tests automatic instance startup.
    guest('poweroff','if [ -S /var/lib/incus/unix.socket ]; then incus stop boot-check --force || true; fi; sync; systemctl poweroff',False)
    vm.wait(timeout=180); vm=None
    serial.close();serial=None

def status(label):
    guest(label,'''zpool status; zpool status -x tanktest | grep -q 'is healthy'; zfs get -H -o value keystatus,mounted tanktest; systemctl --failed --no-pager; systemctl status zfs-import-scan.service zfs-load-keys.service zfs-mount.service --no-pager; journalctl -b -u zfs-import-scan -u zfs-load-keys -u zfs-mount --no-pager; test "$(cat /mnt/tanktest/check)" = 'zfs-reboot-data'; test "$(zfs get -H -o value keystatus tanktest)" = available; test "$(zfs get -H -o value mounted tanktest)" = yes''')
    print('PASS '+label,flush=True)

try:
    raw=work/'system.raw'
    if not raw.exists():
        with raw.open('wb') as f:f.truncate(12*1024**3)
        loop=subprocess.check_output(['losetup','--find','--show','--partscan',str(raw)],text=True).strip()
        print('Installing on disposable '+loop,flush=True)
        log('install',[str(ROOT/'scripts/block_install'),loop,'--local','--image',image,'--target-image','ghcr.io/rosti-cz/dcore:stable','--ssh-key',str(key)+'.pub','--confirm-device',loop])
        esp=loop+'p2'
        mount=work/'esp';mount.mkdir(exist_ok=True)
        run(['mount',esp,str(mount)])
        try:
            for entry in (mount/'loader/entries').glob('*.conf'):
                text=entry.read_text();entry.write_text(text.replace('options ','options console=ttyS0,115200 '))
        finally:run(['umount',str(mount)])
        run(['losetup','--detach',loop]);loop=None
        shutil.copy(firmware_vars,work/'vars.fd')
        for name in ['a','b','c']:
            with (work/(name+'.raw')).open('wb') as f:f.truncate(768*1024**2)
    boot('initial')
    guest('initial-health', '''
    modprobe zfs
    systemctl is-active systemd-networkd.service zfs-import-scan.service zfs-load-keys.service zfs-mount.service
    zfs version
    test ! -e /usr/lib/systemd/system-generators/netplan
    test "$(readlink /etc/systemd/system-generators/netplan)" = /dev/null
    test "$(systemctl is-enabled netplan-ovs-cleanup.service 2>/dev/null || true)" = masked
    systemctl --failed --no-pager
    test -z "$(systemctl --failed --no-legend --no-pager)"
    ''')
    guest('service-regression','DCORE_DISPOSABLE_VM_TEST=1 bash -c '+shlex.quote((ROOT/'tests/test-zfs-boot.sh').read_text()))
    print('PASS service-regression',flush=True)
    setup='''
    install -d -m 700 /etc/zfs/keys
    head -c 32 /dev/urandom > /etc/zfs/keys/tanktest.key
    chmod 600 /etc/zfs/keys/tanktest.key
    for id in a b; do
        disk=/dev/disk/by-id/virtio-zfs-test-$id
        sgdisk --new=1:2048:0 --typecode=1:BF01 "$disk"
    done
    udevadm settle
    zpool create -m /mnt/tanktest -O encryption=on -O keyformat=raw -O keylocation=file:///etc/zfs/keys/tanktest.key tanktest mirror "$(readlink -f /dev/disk/by-id/virtio-zfs-test-a-part1)" "$(readlink -f /dev/disk/by-id/virtio-zfs-test-b-part1)"
    printf 'zfs-reboot-data\\n' > /mnt/tanktest/check
    zfs snapshot tanktest@before-reboot
    zpool set cachefile=/etc/zfs/zpool.cache tanktest
    cp /etc/zfs/zpool.cache /root/stale-zpool.cache
    zpool create -m /mnt/healthytest healthytest /dev/disk/by-id/virtio-zfs-test-c
    echo healthy-pool-data > /mnt/healthytest/check
    systemctl enable --now incus-lxcfs.service incus.socket incus.service incus-startup.service
    incus storage create boot-storage zfs source=tanktest/incus
    incus init boot-check --empty -s boot-storage
    incus config set boot-check raw.lxc='lxc.init.cmd = /bin/sleep infinity'
    incus config set boot-check boot.autostart=true
    incus file push --create-dirs /usr/bin/sleep boot-check/bin/sleep
    incus file push --create-dirs /lib/x86_64-linux-gnu/libc.so.6 boot-check/lib/x86_64-linux-gnu/libc.so.6
    cp -L /lib64/ld-linux-x86-64.so.2 /var/tmp/test-ld.so
    incus file push --create-dirs /var/tmp/test-ld.so boot-check/lib64/ld-linux-x86-64.so.2
    incus start boot-check
    incus list
    sync
    '''
    guest('setup',setup)
    poweroff()
    boot('healthy-reboot')
    status('healthy-reboot-health')
    guest('remove-cache','rm -f /etc/zfs/zpool.cache')
    poweroff()
    boot('absent-cache-reboot')
    status('absent-cache-health')
    guest('stale-cache','cp /root/stale-zpool.cache /etc/zfs/zpool.cache')
    poweroff()
    # Reproduce MD signatures in the member partitions, without modifying ZFS labels.
    source=(ROOT/'tests/test-zfs-boot.sh').read_text().split("<<'PY'\n",1)[1].split('\nPY',1)[0]
    for name in ['a','b']:
        device=subprocess.check_output(['losetup','--find','--show','--partscan',str(work/(name+'.raw'))],text=True).strip()
        try:run(['python3','-c',source,device+'p1'])
        finally:run(['losetup','--detach',device])
    boot('stale-cache-md-swapped-reboot',reverse=True)
    status('stale-cache-md-swapped-health')
    guest('md-evidence','wipefs -n /dev/disk/by-id/virtio-zfs-test-a-part1 /dev/disk/by-id/virtio-zfs-test-b-part1; ls -l /dev/disk/by-id/virtio-zfs-test-*; zpool status -P')
    guest('hide-key','mv /etc/zfs/keys/tanktest.key /etc/zfs/keys/tanktest.key.saved')
    poweroff()
    boot('missing-key-reboot',reverse=True)
    guest('missing-key-health','''test "$(zfs get -H -o value keystatus tanktest)" = unavailable; test "$(zfs get -H -o value mounted tanktest)" = no; test "$(zfs get -H -o value mounted healthytest)" = yes; grep -qx healthy-pool-data /mnt/healthytest/check; systemctl is-failed zfs-load-keys.service; systemctl --failed --no-pager; journalctl -b -u zfs-load-keys -u zfs-mount --no-pager''')
    print('PASS missing-key-reboot',flush=True)
    guest('restore-key','mv /etc/zfs/keys/tanktest.key.saved /etc/zfs/keys/tanktest.key; systemctl restart zfs-load-keys.service zfs-mount.service')
    status('restored-key-health')
    guest('incus-storage-recovery', '''
    for i in $(seq 1 45); do
        if incus list boot-check --format csv -c s | grep -qx RUNNING; then
            incus list
            exit 0
        fi
        sleep 2
    done
    incus list
    exit 1
    ''')
    print('PASS Incus automatic workload recovery after key restoration',flush=True)
    poweroff()
    print('ALL ZFS QEMU TESTS PASSED. Logs: '+str(work),flush=True)
finally:
    if vm is not None:
        vm.terminate()
        try:vm.wait(timeout=30)
        except subprocess.TimeoutExpired:vm.kill();vm.wait()
    if serial is not None:serial.close()
    if loop is not None:subprocess.run(['losetup','--detach',loop])
    os.close(lock)
