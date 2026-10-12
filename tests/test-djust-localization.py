#!/usr/bin/python3
"""Real fzf and systemd-service smoke test; run as root in a disposable dcore container.

Example: podman run --rm --network=none --tmpfs /run --tmpfs /tmp \
    -v "$PWD/tests/test-djust-localization.py:/tmp/test.py:ro" IMAGE python3 /tmp/test.py
"""
import os, pty, select, signal, struct, subprocess, time, fcntl, termios
from pathlib import Path
if not Path('/run/.containerenv').exists() or os.geteuid() != 0:
    raise SystemExit('Run as root only inside a disposable Podman test container.')
subprocess.run(['systemd-tmpfiles', '--create', '/usr/lib/tmpfiles.d/bootc-base-dirs.conf'], check=True)
Path('/run/dbus').mkdir(parents=True, exist_ok=True)
Path('/run/systemd/system').mkdir(parents=True, exist_ok=True)
subprocess.run(['dbus-daemon', '--system', '--fork'], check=True)
daemons = [subprocess.Popen(['/usr/lib/systemd/systemd-'+name]) for name in ('localed', 'timedated')]
time.sleep(.5)
subprocess.run(['runuser', '-u', 'core', '--', 'localectl', '--no-pager', 'list-locales'], check=True, stdout=subprocess.DEVNULL)
def picker(command, query=None):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ['TERM'] = 'xterm-256color'
        os.environ['FZF_DEFAULT_OPTS'] = '--filter=WRONG'
        os.execvp('runuser', ['runuser', '-u', 'core', '--', 'djust', command])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 110, 0, 0))
    output = b''
    deadline = time.monotonic() + 15
    sent = False
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], .1)[0]:
                try: chunk = os.read(fd, 65536)
                except OSError: break
                if not chunk: break
                output += chunk
            if not sent and b'Enter to apply' in output:
                time.sleep(.25)
                os.write(fd, query.encode() if query else b'\x1b')
                if query:
                    time.sleep(.4)
                    os.write(fd, b'\r')
                sent = True
        else:
            raise AssertionError('Picker timed out: ' + repr(output[-1500:]))
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, repr(output[-2000:])
        assert sent, repr(output[-1000:])
        return output
    except BaseException:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except ProcessLookupError: pass
        raise
    finally:
        os.close(fd)
try:
    assert b'System locale set to cs_CZ.UTF-8' in picker('switch-locale', "'cs_CZ.UTF-8$")
    config = Path('/etc/locale.conf').read_bytes()
    assert b'LANG=cs_CZ.UTF-8' in config, config
    assert b'Cancelled; no changes made' in picker('switch-locale')
    assert Path('/etc/locale.conf').read_bytes() == config
    assert b'System timezone set to Europe/Prague' in picker('set-timezone', "'Europe/Prague$")
    target = os.readlink('/etc/localtime')
    assert target.endswith('/Europe/Prague'), target
    assert b'Cancelled; no changes made' in picker('set-timezone')
    assert os.readlink('/etc/localtime') == target
    print('Real fzf selection and Esc cancellation passed as core; localed and timedated applied persistent settings through sudo.')
finally:
    for process in daemons: process.terminate()
