package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

type fakeBackend struct {
	mu       sync.Mutex
	settings *Settings
	layout   map[string]bool
	calls    []string
	fail     string
	failOnce bool
	pause    chan struct{}
	entered  chan struct{}
}

func (b *fakeBackend) Query(op string, value any, result any) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	var response any
	switch op {
	case "status":
		response = map[string]any{"settings": b.settings, "layout": b.layout}
	case "disks":
		response = []any{[]any{map[string]any{"name": "/dev/fixture", "size": int64(20 << 30), "model": "fixture", "serial": "123"}, map[string]any{"device": "/dev/fixture", "diskseq": 42}}}
	case "settings":
		s := value.(*Settings)
		copy := *s
		b.settings = &copy
		return nil
	case "reboot":
		b.calls = append(b.calls, op)
		return nil
	default:
		return errors.New("unsupported operation")
	}
	raw, _ := json.Marshal(response)
	return json.Unmarshal(raw, result)
}
func (b *fakeBackend) Phase(op string, value any, log func(string)) error {
	b.mu.Lock()
	b.calls = append(b.calls, op)
	pause, entered := b.pause, b.entered
	if op != "load" {
		pause = nil
	}
	fail := b.fail == op && b.failOnce
	if fail {
		b.failOnce = false
	}
	if b.layout == nil {
		b.layout = map[string]bool{}
	}
	if op == "install" {
		b.layout["deployed"] = true
		b.layout["clean"] = true
	}
	if op == "configure" && !fail {
		b.layout["configured"] = true
	}
	if op == "verify" && !fail {
		b.layout["verified"] = true
	}
	b.mu.Unlock()
	if pause != nil {
		close(entered)
		<-pause
	}
	log("backend output")
	if fail {
		return errors.New("injected failure")
	}
	return nil
}
func testEngine(t *testing.T, b *fakeBackend) *Engine {
	t.Helper()
	e, err := newEngine(filepath.Join(t.TempDir(), "session"), b)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(e.Close)
	if _, err = e.Disks(); err != nil {
		t.Fatal(err)
	}
	return e
}
func testInput(e *Engine) Input {
	return Input{Device: "/dev/fixture", Filesystem: "btrfs", Encrypt: true, CorePassword: "private-core", CoreConfirm: "private-core", LUKSPassphrase: "private-disk", LUKSConfirm: "private-disk", Confirmed: true, Revision: e.Snapshot().Revision}
}
func TestRetryDoesNotRepeatDeployment(t *testing.T) {
	b := &fakeBackend{fail: "configure", failOnce: true}
	e := testEngine(t, b)
	if err := e.Start(testInput(e)); err != nil {
		t.Fatal(err)
	}
	e.wg.Wait()
	if e.Snapshot().Failed != "configure" {
		t.Fatal(e.Snapshot())
	}
	if err := e.Start(testInput(e)); err != nil {
		t.Fatal(err)
	}
	e.wg.Wait()
	if !e.Snapshot().Done {
		t.Fatal(e.Snapshot())
	}
	if strings.Count(strings.Join(b.calls, ","), "install") != 1 {
		t.Fatal(b.calls)
	}
}
func TestNoDuplicateStart(t *testing.T) {
	b := &fakeBackend{pause: make(chan struct{}), entered: make(chan struct{})}
	e := testEngine(t, b)
	input := testInput(e)
	if err := e.Start(input); err != nil {
		t.Fatal(err)
	}
	<-b.entered
	if err := e.Start(input); err == nil {
		t.Error("duplicate start accepted")
	}
	close(b.pause)
	e.wg.Wait()
	if err := e.Start(input); err == nil {
		t.Error("stale start accepted")
	}
}
func TestCheckpointsNeverContainPasswords(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	if err := e.Start(testInput(e)); err != nil {
		t.Fatal(err)
	}
	e.wg.Wait()
	raw, err := os.ReadFile(filepath.Join(e.directory, "session.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"private-core", "private-disk"} {
		if strings.Contains(string(raw), secret) {
			t.Fatal("secret persisted")
		}
	}
	if info, _ := os.Stat(filepath.Join(e.directory, "session.json")); info.Mode().Perm() != 0600 {
		t.Fatal(info.Mode())
	}
}
func TestDeploymentFailureRequiresCleanup(t *testing.T) {
	b := &fakeBackend{fail: "install", failOnce: true}
	e := testEngine(t, b)
	if err := e.Start(testInput(e)); err != nil {
		t.Fatal(err)
	}
	e.wg.Wait()
	if e.Snapshot().Failed != "cleanup" || !contains(e.Snapshot().Completed, "install") {
		t.Fatal(e.Snapshot())
	}
	if err := e.Start(testInput(e)); err != nil {
		t.Fatal(err)
	}
	e.wg.Wait()
	if !e.Snapshot().Done || strings.Count(strings.Join(b.calls, ","), "install") != 1 {
		t.Fatal(e.Snapshot(), b.calls)
	}
}
func TestFilesystemEncryptionMatrix(t *testing.T) {
	for _, fs := range []string{"ext4", "btrfs", "xfs"} {
		for _, encrypt := range []bool{false, true} {
			e := testEngine(t, &fakeBackend{})
			input := testInput(e)
			input.Filesystem = fs
			input.Encrypt = encrypt
			if err := e.Start(input); err != nil {
				t.Fatal(err)
			}
			e.wg.Wait()
			if !e.Snapshot().Done {
				t.Fatal(e.Snapshot())
			}
		}
	}
}
func TestBadInputsNeverStart(t *testing.T) {
	for _, mutate := range []func(*Input){func(i *Input) { i.Confirmed = false }, func(i *Input) { i.CoreConfirm = "wrong" }, func(i *Input) { i.LUKSConfirm = "wrong" }, func(i *Input) { i.Filesystem = "zfs" }, func(i *Input) { i.Device = "/dev/other" }, func(i *Input) { i.CorePassword = "x\n"; i.CoreConfirm = "x\n" }} {
		b := &fakeBackend{}
		e := testEngine(t, b)
		input := testInput(e)
		mutate(&input)
		if err := e.Start(input); err == nil {
			t.Fatal("invalid input accepted")
		}
		if len(b.calls) != 0 {
			t.Fatal(b.calls)
		}
	}
}
func TestSessionExclusive(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	if _, err := newEngine(e.directory, &fakeBackend{}); err == nil {
		t.Fatal("second session allowed")
	}
}
func TestRebootOnlyCompletedISO(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	if err := e.Reboot(); err == nil {
		t.Fatal("host reboot allowed")
	}
	e.state.CanReboot = true
	if err := e.Reboot(); err == nil {
		t.Fatal("incomplete reboot allowed")
	}
	e.state.Done = true
	if err := e.Reboot(); err != nil {
		t.Fatal(err)
	}
}

type metadataBackend struct{ fakeBackend }

func (b *metadataBackend) Query(op string, value any, result any) error {
	if op == "status" {
		raw := []byte(`{"settings":{"device":"/dev/fixture","identity":{"device":"/dev/fixture"},"filesystem":"btrfs","encrypt":true},"layout":{"version":1,"device":"/dev/fixture","root_partition":"/dev/vda3","root_uuid":"abc123","mapper":null,"deployed":true,"configured":true,"verified":false,"clean":false}}`)
		return json.Unmarshal(raw, result)
	}
	return b.fakeBackend.Query(op, value, result)
}
func TestRestartRecoversRealLayoutMetadataWithoutSecrets(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "session")
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	checkpoint := []byte(`{"completed":["load","validate","install"],"running":"configure","revision":5}`)
	if err := os.WriteFile(filepath.Join(directory, "session.json"), checkpoint, 0600); err != nil {
		t.Fatal(err)
	}
	e, err := newEngine(directory, &metadataBackend{})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	s := e.Snapshot()
	if s.Failed != "cleanup" || !contains(s.Completed, "configure") || s.Settings == nil {
		t.Fatal(s)
	}
	raw, _ := json.Marshal(s)
	if strings.Contains(string(raw), "core_password") {
		t.Fatal("credentials exposed")
	}
}

func TestLocalRescueBlocksRemoteInstallation(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	if err := e.BeginRescue(); err != nil {
		t.Fatal(err)
	}
	if e.Snapshot().Running != "rescue" {
		t.Fatal(e.Snapshot())
	}
	if err := e.Start(testInput(e)); err == nil {
		t.Fatal("remote deployment allowed during local rescue")
	}
	e.EndRescue()
	if err := e.Start(testInput(e)); err != nil {
		t.Fatal(err)
	}
	e.wg.Wait()
}
