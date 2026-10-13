package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/charmbracelet/x/ansi"
	"golang.org/x/sys/unix"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"unicode/utf8"
)

type Disk struct {
	Name     string `json:"name"`
	Size     int64  `json:"size"`
	Model    string `json:"model"`
	Serial   string `json:"serial"`
	identity json.RawMessage
}
type Settings struct {
	Device     string          `json:"device"`
	Identity   json.RawMessage `json:"identity"`
	Filesystem string          `json:"filesystem"`
	Encrypt    bool            `json:"encrypt"`
}
type Input struct {
	Device         string `json:"device"`
	Filesystem     string `json:"filesystem"`
	Encrypt        bool   `json:"encrypt"`
	CorePassword   string `json:"core_password"`
	CoreConfirm    string `json:"core_confirm"`
	LUKSPassphrase string `json:"luks_passphrase"`
	LUKSConfirm    string `json:"luks_confirm"`
	Confirmation   string `json:"confirmation"`
	Revision       uint64 `json:"revision"`
}
type Snapshot struct {
	Settings  *Settings `json:"settings"`
	Completed []string  `json:"completed"`
	Running   string    `json:"running"`
	Failed    string    `json:"failed"`
	Error     string    `json:"error"`
	Logs      []string  `json:"logs"`
	Revision  uint64    `json:"revision"`
	Done      bool      `json:"done"`
	CanReboot bool      `json:"can_reboot"`
}
type Backend interface {
	Query(string, any, any) error
	Phase(string, any, func(string)) error
}
type commandBackend struct{ live bool }

func (b commandBackend) command(operation string) *exec.Cmd {
	args := []string{"/usr/libexec/dcore/installer/host-backend.py"}
	if b.live {
		args = append(args, "--live")
	}
	args = append(args, operation)
	if !b.live {
		args = append([]string{"--non-interactive", "--"}, args...)
		return exec.Command("/usr/bin/sudo", args...)
	}
	return exec.Command(args[0], args[1:]...)
}
func (b commandBackend) Query(operation string, value any, result any) error {
	cmd := b.command(operation)
	if value != nil {
		raw, err := json.Marshal(value)
		if err != nil {
			return err
		}
		cmd.Stdin = bytes.NewReader(raw)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("%s: %s", operation, strings.TrimSpace(stderr.String()))
	}
	if result != nil {
		return json.Unmarshal(out, result)
	}
	return nil
}
func (b commandBackend) Phase(operation string, value any, log func(string)) error {
	cmd := b.command(operation)
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	cmd.Stdin = bytes.NewReader(raw)
	out, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = cmd.Stdout
	if err = cmd.Start(); err != nil {
		return err
	}
	scanner := bufio.NewScanner(out)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	for scanner.Scan() {
		log(scanner.Text())
	}
	scanerr := scanner.Err()
	if scanerr != nil {
		io.Copy(io.Discard, out)
	}
	err = cmd.Wait()
	if scanerr != nil {
		return scanerr
	}
	if err != nil {
		return fmt.Errorf("%s failed; inspect the log and retry", operation)
	}
	return nil
}

var phases = []string{"load", "validate", "install", "configure", "verify", "cleanup"}

type Engine struct {
	mu         sync.Mutex
	state      Snapshot
	backend    Backend
	directory  string
	lock       *os.File
	disks      []Disk
	active     bool
	enrollment bool
	wg         sync.WaitGroup
}

func newEngine(directory string, backend Backend) (*Engine, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(directory)
	if err != nil {
		return nil, err
	}
	var ownership unix.Stat_t
	if err = unix.Lstat(directory, &ownership); err != nil {
		return nil, err
	}
	if ownership.Uid != uint32(os.Geteuid()) {
		return nil, errors.New("installer state directory must be owned by the caller")
	}
	if !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("installer state directory must be private")
	}
	fd, err := unix.Open(filepath.Join(directory, "session.lock"), unix.O_CREAT|unix.O_RDWR|unix.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	if err = unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB); err != nil {
		unix.Close(fd)
		return nil, errors.New("another installer session is running")
	}
	e := &Engine{backend: backend, directory: directory, lock: os.NewFile(uintptr(fd), "session.lock"), state: Snapshot{Revision: 1, Completed: []string{}}}
	if raw, err := os.ReadFile(filepath.Join(directory, "session.json")); err == nil {
		if err = json.Unmarshal(raw, &e.state); err != nil {
			e.Close()
			return nil, err
		}
		if e.state.Running != "" {
			e.state.Failed = e.state.Running
			e.state.Running = ""
			e.state.Error = "The previous interface stopped during this step. Inspect the log before retrying."
		}
	}
	if err = e.reconcile(); err != nil {
		e.Close()
		return nil, err
	}
	return e, nil
}
func (e *Engine) Close() {
	e.wg.Wait()
	if e.lock != nil {
		e.lock.Close()
		e.lock = nil
	}
}
func (e *Engine) Snapshot() Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	raw, _ := json.Marshal(e.state)
	var copy Snapshot
	json.Unmarshal(raw, &copy)
	return copy
}
func (e *Engine) save() error {
	raw, err := json.Marshal(e.state)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(e.directory, ".session-")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err = f.Write(raw); err == nil {
		err = f.Sync()
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(name, filepath.Join(e.directory, "session.json"))
}
func contains(values []string, value string) bool {
	for _, v := range values {
		if v == value {
			return true
		}
	}
	return false
}
func (e *Engine) reconcile() error {
	var status struct {
		Layout   map[string]any `json:"layout"`
		Settings *Settings      `json:"settings"`
	}
	if err := e.backend.Query("status", nil, &status); err != nil {
		return err
	}
	if status.Settings != nil {
		e.state.Settings = status.Settings
	}
	if status.Layout["deployed"] == true {
		for _, phase := range []string{"load", "validate", "install"} {
			if !contains(e.state.Completed, phase) {
				e.state.Completed = append(e.state.Completed, phase)
			}
		}
		for field, phase := range map[string]string{"configured": "configure", "verified": "verify"} {
			if status.Layout[field] == true && !contains(e.state.Completed, phase) {
				e.state.Completed = append(e.state.Completed, phase)
			}
		}
		if e.state.Failed == "install" || ((e.state.Failed == "configure" || e.state.Failed == "verify") && status.Layout["clean"] != true) {
			e.state.Failed = "cleanup"
		}
	}
	e.state.Done = len(e.state.Completed) == len(phases)
	return e.save()
}
func (e *Engine) Disks() ([]Disk, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.active {
		return append([]Disk(nil), e.disks...), nil
	}
	var rows []json.RawMessage
	if err := e.backend.Query("disks", nil, &rows); err != nil {
		return nil, err
	}
	disks := []Disk{}
	for _, row := range rows {
		var pair []json.RawMessage
		if err := json.Unmarshal(row, &pair); err != nil || len(pair) != 2 {
			return nil, errors.New("invalid backend disk response")
		}
		var disk Disk
		if err := json.Unmarshal(pair[0], &disk); err != nil {
			return nil, err
		}
		disk.identity = pair[1]
		disks = append(disks, disk)
	}
	e.disks = disks
	return disks, nil
}
func password(value string) error {
	if value == "" || len(value) > 4096 || !utf8.ValidString(value) || strings.ContainsAny(value, "\x00\n\r") {
		return errors.New("password must be nonempty, valid UTF-8, without line breaks, and at most 4096 bytes")
	}
	return nil
}
func (e *Engine) validate(input Input) error {
	if input.Revision != e.state.Revision {
		return errors.New("session changed; refresh before submitting")
	}
	if e.active || e.enrollment {
		return errors.New("another operation is running")
	}
	if input.Filesystem != "ext4" && input.Filesystem != "btrfs" && input.Filesystem != "xfs" {
		return errors.New("choose ext4, btrfs, or xfs")
	}
	if err := password(input.CorePassword); err != nil {
		return err
	}
	if input.CorePassword != input.CoreConfirm {
		return errors.New("administrator passwords do not match")
	}
	if input.Encrypt {
		if err := password(input.LUKSPassphrase); err != nil {
			return err
		}
		if input.LUKSPassphrase != input.LUKSConfirm {
			return errors.New("disk passphrases do not match")
		}
	}
	return nil
}
func (e *Engine) configure(input Input) error {
	if err := e.validate(input); err != nil {
		return err
	}
	if contains(e.state.Completed, "install") {
		s := e.state.Settings
		if s == nil || s.Device != input.Device || s.Filesystem != input.Filesystem || s.Encrypt != input.Encrypt {
			return errors.New("deployment already started; settings cannot change")
		}
		return nil
	}
	var chosen *Disk
	for i := range e.disks {
		if e.disks[i].Name == input.Device {
			chosen = &e.disks[i]
			break
		}
	}
	if chosen == nil {
		return errors.New("select an eligible destination disk")
	}
	settings := &Settings{Device: input.Device, Identity: chosen.identity, Filesystem: input.Filesystem, Encrypt: input.Encrypt}
	if err := e.backend.Query("settings", settings, nil); err != nil {
		return err
	}
	e.state.Settings = settings
	completed := []string{}
	if contains(e.state.Completed, "load") {
		completed = append(completed, "load")
	}
	e.state.Completed = completed
	e.state.Failed = ""
	e.state.Error = ""
	e.state.Revision++
	return e.save()
}
func (e *Engine) Configure(input Input) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.configure(input)
}
func (e *Engine) Start(input Input) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.state.Done {
		return errors.New("installation is already complete")
	}
	if err := e.validate(input); err != nil {
		return err
	}
	if !contains(e.state.Completed, "install") && input.Confirmation != "ERASE "+input.Device {
		return errors.New("type ERASE followed by the exact destination path")
	}
	if err := e.configure(input); err != nil {
		return err
	}
	e.active = true
	e.state.Running = "preparing"
	e.state.Revision++
	if err := e.save(); err != nil {
		e.active = false
		e.state.Running = ""
		return err
	}
	e.wg.Add(1)
	go e.run(input)
	return nil
}
func sanitize(line string) string {
	line = ansi.Strip(line)
	return strings.Map(func(r rune) rune {
		if (r < 32 && r != '\t') || r == 127 {
			return -1
		}
		return r
	}, line)
}
func redact(line string, secrets ...string) string {
	line = sanitize(line)
	for _, secret := range secrets {
		for _, pattern := range []string{secret, sanitize(secret)} {
			if pattern != "" {
				line = strings.ReplaceAll(line, pattern, "[redacted]")
			}
		}
	}
	return line
}
func (e *Engine) log(line string) {
	line = sanitize(line)
	if len(line) > 16384 {
		line = line[:16384] + " [truncated]"
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state.Logs = append(e.state.Logs, line)
	if len(e.state.Logs) > 1000 {
		e.state.Logs = e.state.Logs[len(e.state.Logs)-1000:]
	}
}
func (e *Engine) run(input Input) {
	defer e.wg.Done()
	defer func() {
		e.mu.Lock()
		e.active = false
		e.state.Running = ""
		e.state.Revision++
		e.save()
		e.mu.Unlock()
	}()
	log := func(line string) { e.log(redact(line, input.CorePassword, input.LUKSPassphrase)) }
	fail := func(phase string, err error) {
		e.fail(phase, errors.New(redact(err.Error(), input.CorePassword, input.LUKSPassphrase)))
	}
	e.mu.Lock()
	cleanup := e.state.Failed == "cleanup"
	e.mu.Unlock()
	if cleanup {
		if err := e.backend.Phase("cleanup", map[string]any{}, log); err != nil {
			fail("cleanup", err)
			return
		}
	}
	for _, phase := range phases {
		e.mu.Lock()
		if contains(e.state.Completed, phase) {
			e.mu.Unlock()
			continue
		}
		e.state.Running = phase
		e.state.Failed = ""
		e.state.Error = ""
		e.state.Revision++
		err := e.save()
		e.mu.Unlock()
		if err != nil {
			fail(phase, err)
			return
		}
		log("--- " + phase + " ---")
		request := map[string]any{}
		if phase == "install" || phase == "configure" || phase == "verify" {
			request["core_password"] = input.CorePassword
			if input.Encrypt {
				request["luks_passphrase"] = input.LUKSPassphrase
			}
		}
		if phase == "install" {
			request["confirmation"] = input.Confirmation
		}
		if err = e.backend.Phase(phase, request, log); err != nil {
			fail(phase, err)
			return
		}
		e.mu.Lock()
		e.state.Completed = append(e.state.Completed, phase)
		e.state.Running = ""
		err = e.save()
		e.mu.Unlock()
		if err != nil {
			fail(phase, err)
			return
		}
	}
	e.mu.Lock()
	e.state.Done = true
	e.mu.Unlock()
}
func (e *Engine) fail(phase string, err error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state.Failed = phase
	e.state.Error = err.Error()
	e.state.Running = ""
	if reconcileErr := e.reconcile(); reconcileErr != nil {
		e.state.Error += "; checkpoint: " + reconcileErr.Error()
	}
	e.save()
}
func (e *Engine) Enroll(first, second string) error {
	if first != second || len(first) < 1 || len(first) > 16 || strings.ContainsAny(first, "\n\r\x00") {
		return errors.New("enrollment passwords must match and contain 1–16 ASCII characters")
	}
	for _, c := range first {
		if c < 32 || c > 126 {
			return errors.New("enrollment password must use printable ASCII")
		}
	}
	e.mu.Lock()
	if !e.state.Done {
		e.mu.Unlock()
		return errors.New("complete installation before enrolling the Secure Boot key")
	}
	if e.active || e.enrollment {
		e.mu.Unlock()
		return errors.New("another operation is running")
	}
	e.enrollment = true
	e.state.Running = "enroll"
	e.state.Revision++
	e.wg.Add(1)
	e.mu.Unlock()
	defer func() {
		e.mu.Lock()
		e.enrollment = false
		e.state.Running = ""
		e.state.Revision++
		e.save()
		e.mu.Unlock()
		e.wg.Done()
	}()
	err := e.backend.Phase("enroll", map[string]string{"enrollment_password": first, "enrollment_confirm": second}, func(line string) { e.log(redact(line, first)) })
	if err != nil {
		return errors.New(redact(err.Error(), first))
	}
	return nil
}

func (e *Engine) Reboot() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.state.CanReboot || !e.state.Done || e.active || e.enrollment {
		return errors.New("reboot is available only after a successful live ISO installation")
	}
	return e.backend.Query("reboot", nil, nil)
}

func (e *Engine) BeginRescue() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.active || e.enrollment {
		return errors.New("wait for the active operation before opening a rescue shell")
	}
	e.active = true
	e.state.Running = "rescue"
	e.state.Revision++
	e.wg.Add(1)
	return nil
}
func (e *Engine) EndRescue() {
	e.mu.Lock()
	e.active = false
	e.state.Running = ""
	e.state.Revision++
	e.mu.Unlock()
	e.wg.Done()
}
