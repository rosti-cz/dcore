"""Textual interface for the recoverable offline installer."""
import os
from pathlib import Path
import subprocess

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, ProgressBar, RichLog, Select, Static, TextArea

from session import InstallerSession, STEPS


class EraseConfirmation(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    CSS = '''
    EraseConfirmation { align: center middle; background: $background 80%; }
    #confirm-box { width: 72; max-width: 95%; height: auto; border: thick $warning; padding: 1 2; }
    #confirm-box Static { height: auto; margin-bottom: 1; }
    #confirm-buttons { height: 3; }
    #confirm-buttons Button { margin-right: 1; }
    '''

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        with Container(id='confirm-box'):
            yield Static('Confirm disk erasure', classes='heading')
            yield Static(f"Drive: {self.settings['device']}\nFilesystem: {self.settings['filesystem']}\n"
                         f"Encryption: {'LUKS2' if self.settings['encrypt'] else 'none'}\n\n"
                         'All existing data on this drive will be erased.', markup=False)
            yield Input(placeholder=f"Type ERASE {self.settings['device']}", id='erase')
            yield Static('', id='confirmation-error')
            with Horizontal(id='confirm-buttons'):
                yield Button('Erase and install', variant='error', id='confirm-erase')
                yield Button('Back', id='cancel-erase')

    def action_cancel(self):
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == 'erase':
            self.confirm()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'confirm-erase':
            self.confirm()
        else:
            self.dismiss(None)

    def confirm(self):
        answer = self.query_one('#erase', Input).value
        if answer != f"ERASE {self.settings['device']}":
            self.query_one('#confirmation-error', Static).update('The confirmation must match the selected drive exactly.')
            return
        self.dismiss(answer)


class LogView(ModalScreen):
    BINDINGS = [('escape', 'close', 'Back')]
    CSS = 'LogView { padding: 1 2; } TextArea { height: 1fr; }'

    def __init__(self, contents):
        super().__init__()
        self.contents = contents

    def compose(self):
        yield Label('Installation log — Escape to return')
        yield TextArea(self.contents, read_only=True)

    def action_close(self):
        self.dismiss()


class InstallerApp(App):
    TITLE = 'dcore installer'
    AUTO_FOCUS = '#disks'
    BINDINGS = [('ctrl+r', 'retry', 'Retry step'), ('ctrl+l', 'logs', 'Logs'),
                ('ctrl+c', 'request_exit', 'Exit')]
    CSS = '''
    Screen { background: #0b1721; }
    Header { background: #174a60; }
    .heading { text-style: bold; color: #6adacb; }
    #settings { padding: 0 2; }
    #disks { height: 6; margin-bottom: 1; }
    #filesystem { width: 1fr; }
    #encrypt { width: 1fr; }
    #choices { height: 3; }
    #unlock-fields { height: auto; }
    #settings Input { margin-bottom: 1; }
    #form-error { color: $error; height: auto; }
    #settings-buttons { height: 3; }
    #settings-buttons Button { margin-right: 1; }
    #progress { display: none; padding: 0 2; }
    #summary, #step-list, #status { height: auto; }
    #status { color: $warning; margin-bottom: 1; }
    #step-list { margin-bottom: 1; }
    #output { height: 1fr; min-height: 3; border: solid #174a60; }
    #progress-buttons, #boot-buttons { height: 3; margin-top: 1; }
    #progress-buttons Button, #boot-buttons Button { margin-right: 1; min-width: 10; }
    '''

    def __init__(self, session=None):
        super().__init__()
        self.session = session or InstallerSession()
        self.busy = False
        self.disk_choices = []
        self.log_file = self.session.directory / 'install.log'

    def compose(self):
        yield Header()
        with VerticalScroll(id='settings'):
            yield Label('Choose the system drive', classes='heading')
            yield DataTable(id='disks', cursor_type='row')
            with Horizontal(id='choices'):
                yield Select([('ext4 (default)', 'ext4'), ('Btrfs', 'btrfs'), ('XFS', 'xfs')],
                             value='ext4', allow_blank=False, id='filesystem')
                yield Checkbox('Encrypt with LUKS2', id='encrypt')
            yield Label('Core password — console and SSH login')
            yield Input(password=True, placeholder='Core password', id='core-password')
            yield Input(password=True, placeholder='Confirm core password', id='core-confirm')
            with Container(id='unlock-fields'):
                yield Label('Disk unlock passphrase — required at every boot')
                yield Input(password=True, placeholder='Separate disk passphrase', id='disk-password')
                yield Input(password=True, placeholder='Confirm disk passphrase', id='disk-confirm')
            yield Static('', id='form-error', markup=False)
            with Horizontal(id='settings-buttons'):
                yield Button('Review installation', variant='primary', id='review')
                yield Button('Rescan drives', id='rescan')
                yield Button('Rescue shell', id='settings-shell')
        with VerticalScroll(id='progress'):
            yield Static('', id='summary', markup=False)
            yield Static('', id='step-list', markup=False)
            yield ProgressBar(total=len(STEPS), show_eta=False, id='bar')
            yield Static('', id='status', markup=False)
            yield RichLog(id='output', wrap=True, markup=False, max_lines=1000)
            with Horizontal(id='progress-buttons'):
                yield Button('Retry step', variant='primary', id='retry')
                yield Button('Passwords', id='passwords')
                yield Button('View logs', id='logs')
                yield Button('Shell', id='shell')
            with Horizontal(id='boot-buttons'):
                yield Button('Enroll key', id='enroll-key', disabled=True)
                yield Button('Reboot', variant='success', id='reboot', disabled=True)

        yield Footer()

    def on_mount(self):
        if getattr(self.session, 'HOST_MODE', False):
            self.query_one('#reboot').display = False
        table = self.query_one('#disks', DataTable)
        table.add_columns('Drive', 'Size', 'Model', 'Serial')
        saved = self.session.state.get('settings')
        if saved:
            self.query_one('#filesystem', Select).value = saved['filesystem']
            self.query_one('#encrypt', Checkbox).value = saved['encrypt']
        self.rescan()
        self.query_one('#unlock-fields').display = bool(saved and saved['encrypt'])
        if self.session.layout():
            table.disabled = True
            self.query_one('#filesystem').disabled = True
            self.query_one('#encrypt').disabled = True
            self.query_one('#rescan').disabled = True
            self.query_one('#review', Button).label = 'Resume installation'
            self.query_one('#core-password', Input).focus()
            self.query_one('#form-error', Static).update('OS deployment is saved. Re-enter passwords to resume; the disk will not be erased.')
        try:
            self.session.check_resources()
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            self.query_one('#form-error', Static).update(str(error))
            self.query_one('#review').disabled = True
        if len(self.session.state['completed']) == len(STEPS):
            self.query_one('#settings').display = False
            self.query_one('#progress').display = True
            self.finished(True)

    def rescan(self):
        table = self.query_one('#disks', DataTable)
        table.clear()
        saved = self.session.state.get('settings')
        try:
            if self.session.layout() and saved:
                self.disk_choices = [({'name': saved['device'], 'size': saved['identity']['size'],
                                      'model': 'Saved deployment', 'serial': ''}, saved['identity'])]
            else:
                self.disk_choices = self.session.disks()
            for disk, identity in self.disk_choices:
                table.add_row(disk['name'], f"{int(disk['size']) / 1024**3:.1f} GiB",
                              disk.get('model') or '', disk.get('serial') or '')
            if not self.disk_choices:
                self.query_one('#form-error', Static).update('No unused whole drives of at least 10 GiB. Rescan after attaching a drive, or open a rescue shell.')
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            self.query_one('#form-error', Static).update(str(error))

    def on_checkbox_changed(self, event: Checkbox.Changed):
        if event.checkbox.id == 'encrypt':
            event.checkbox.screen.query_one('#unlock-fields').display = event.value

    def review(self):
        try:
            if not self.disk_choices:
                raise ValueError('Select a destination drive first.')
            password = self.query_one('#core-password', Input).value
            if password != self.query_one('#core-confirm', Input).value:
                raise ValueError('Core passwords do not match.')
            encrypt = self.query_one('#encrypt', Checkbox).value
            passphrase = self.query_one('#disk-password', Input).value if encrypt else None
            if encrypt and passphrase != self.query_one('#disk-confirm', Input).value:
                raise ValueError('Disk passphrases do not match.')
            if not self.session.layout():
                index = self.query_one('#disks', DataTable).cursor_row
                disk, identity = self.disk_choices[index]
                self.session.settings(disk['name'], identity, self.query_one('#filesystem', Select).value, encrypt)
            self.session.credentials(password.encode('utf-8'), passphrase.encode('utf-8') if passphrase is not None else None)
            self.query_one('#form-error', Static).update('')
        except (ValueError, OSError, IndexError) as error:
            self.query_one('#form-error', Static).update(str(error))
            return
        if self.session.layout():
            self.start(None)
        else:
            self.push_screen(EraseConfirmation(self.session.state['settings']), self.start)

    def start(self, confirmation):
        if confirmation is None and not self.session.layout():
            return
        self.query_one('#settings').display = False
        self.query_one('#progress').display = True
        self.busy = True
        self.refresh_progress()
        self.execute(confirmation)

    @work(thread=True)
    def execute(self, confirmation):
        try:
            success = self.session.run(confirmation, log=lambda line: self.call_from_thread(self.append_log, line),
                                       changed=lambda: self.call_from_thread(self.refresh_progress))
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            self.session.state['error'] = str(error)
            self.session.save()
            success = False
        self.call_from_thread(self.finished, success)

    def append_log(self, line):
        self.query_one('#output', RichLog).write(line)
        descriptor = os.open(self.log_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as output:
            output.write(line + '\n')

    def refresh_progress(self):
        state = self.session.state
        saved = state['settings']
        if saved:
            self.query_one('#summary', Static).update(f"{saved['device']}  |  {saved['filesystem']}  |  {'LUKS2' if saved['encrypt'] else 'unencrypted'}")
        rows = []
        for phase, title in STEPS:
            if phase == 'load' and getattr(self.session, 'HOST_MODE', False):
                title = 'Prepare the booted installation image'
            status = 'DONE' if phase in state['completed'] else 'FAIL' if phase == state.get('failed') else 'RUN' if phase == state.get('running') else 'WAIT'
            rows.append(f'[{status:4}] {title}')
        self.query_one('#step-list', Static).update('\n'.join(rows))
        self.query_one('#bar', ProgressBar).update(progress=len(state['completed']))
        self.query_one('#status', Static).update(state.get('error') or ('Working…' if self.busy else 'Ready'))
        for id in ('retry', 'passwords', 'logs', 'shell'):
            self.query_one('#' + id).disabled = self.busy
        self.query_one('#reboot').disabled = self.busy or len(state['completed']) != len(STEPS)
        self.query_one('#enroll-key').disabled = self.busy or len(state['completed']) != len(STEPS)

    def finished(self, success):
        self.busy = False
        self.refresh_progress()
        if success:
            message = ('Installation complete. Boot the selected drive using the target machine’s UEFI boot menu.'
                       if getattr(self.session, 'HOST_MODE', False) else
                       'Installation complete. Remove installer media before rebooting.')
            self.query_one('#status', Static).update(message + ' Secure Boot: use Enroll key to trust ZFS modules on this machine; confirm in MokManager on reboot.')

    def action_retry(self):
        if self.busy or self.query_one('#progress').display is False:
            return
        if self.session.core_password is None:
            self.password_screen()
        elif 'install' not in self.session.state['completed']:
            self.push_screen(EraseConfirmation(self.session.state['settings']), self.start)
        else:
            self.start(None)

    def password_screen(self):
        self.query_one('#progress').display = False
        self.query_one('#settings').display = True
        if self.session.layout():
            for id in ('disks', 'filesystem', 'encrypt', 'rescan'):
                self.query_one('#' + id).disabled = True
            self.query_one('#review', Button).label = 'Resume installation'

    def action_logs(self):
        if not self.busy:
            contents = self.log_file.read_text() if self.log_file.exists() else 'No installation output yet.'
            self.push_screen(LogView(contents))

    def shell(self):
        if self.busy:
            return
        with self.suspend():
            print('\033[2J\033[H', end='', flush=True)
            print('dcore rescue shell. Type exit to return to the installer.')
            subprocess.run(['/bin/bash'], check=False)
        self.session.reconcile()
        self.refresh_progress()

    def action_quit(self):
        self.action_request_exit()

    def action_request_exit(self):
        if self.busy:
            self.notify('Wait for this operation to finish before exiting.', severity='warning')
        else:
            self.exit()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'enroll-key' and not self.busy and len(self.session.state['completed']) == len(STEPS):
            command = ['/usr/libexec/dcore/enroll-secureboot']
            if os.geteuid() != 0:
                command = ['/usr/bin/sudo', '--', *command]
            with self.suspend():
                try:
                    succeeded = subprocess.run(command).returncode == 0
                except (OSError, KeyboardInterrupt):
                    succeeded = False
                try:
                    input('Press Enter to return to the installer…')
                except (EOFError, KeyboardInterrupt):
                    pass
            self.query_one('#status', Static).update('Enrollment step finished; confirm any requested key on reboot.'
                if succeeded else 'Enrollment failed. Use Enroll key to retry; the installed disk is preserved.')
            return
        actions = {'review': self.review, 'rescan': self.rescan, 'retry': self.action_retry,
                   'passwords': self.password_screen, 'logs': self.action_logs,
                   'shell': self.shell, 'settings-shell': self.shell}
        if event.button.id == 'reboot' and not getattr(self.session, 'HOST_MODE', False) and not self.busy and len(self.session.state['completed']) == len(STEPS):
            subprocess.run(['systemctl', 'reboot'], check=True)
        elif event.button.id in actions:
            actions[event.button.id]()
