package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type reviewBackend struct {
	status  string
	line    string
	failure error
}

func (b reviewBackend) Query(operation string, _ any, result any) error {
	if result == nil {
		return nil
	}
	raw := b.status
	if operation == "disks" {
		raw = `[[{"name":"/dev/test","size":10737418240},{"device":"/dev/test"}]]`
	}
	return json.Unmarshal([]byte(raw), result)
}
func (b reviewBackend) Phase(phase string, _ any, log func(string)) error {
	log(b.line)
	if phase == "install" {
		return b.failure
	}
	return nil
}
func privateReviewDirectory(t *testing.T) string {
	t.Helper()
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	return directory
}

func TestNormalizedLogsAndBackendErrorsNeverExposeSecrets(t *testing.T) {
	for _, test := range []struct {
		name, line string
		failure    error
	}{
		{"colored log", "core-\x1b[31msecret\x1b[0m and disk-\x1b[32msecret\x1b[0m", nil},
		{"backend error", "safe log", errors.New("operation failed with core-secret and disk-secret")},
	} {
		t.Run(test.name, func(t *testing.T) {
			directory := privateReviewDirectory(t)
			e, err := newEngine(directory, reviewBackend{status: `{"layout":{}}`, line: test.line, failure: test.failure})
			if err != nil {
				t.Fatal(err)
			}
			defer e.Close()
			if _, err = e.Disks(); err != nil {
				t.Fatal(err)
			}
			input := Input{Device: "/dev/test", Filesystem: "ext4", Encrypt: true, CorePassword: "core-secret", CoreConfirm: "core-secret", LUKSPassphrase: "disk-secret", LUKSConfirm: "disk-secret", Confirmation: "ERASE /dev/test", Revision: e.Snapshot().Revision}
			if err = e.Start(input); err != nil {
				t.Fatal(err)
			}
			e.wg.Wait()
			raw, err := os.ReadFile(filepath.Join(directory, "session.json"))
			if err != nil {
				t.Fatal(err)
			}
			for _, secret := range []string{"core-secret", "disk-secret"} {
				if strings.Contains(string(raw), secret) {
					t.Fatalf("secret persisted in checkpoint: %q", secret)
				}
			}
		})
	}
}

func TestDeployedBackendSettingsOverrideStaleUICheckpoint(t *testing.T) {
	directory := privateReviewDirectory(t)
	raw := `{"settings":{"device":"/dev/old","filesystem":"ext4","encrypt":false},"completed":["load"],"revision":1}`
	if err := os.WriteFile(filepath.Join(directory, "session.json"), []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	backend := reviewBackend{status: `{"layout":{"deployed":true,"configured":false,"clean":true,"device":"/dev/new","filesystem":"btrfs","encrypted":true},"settings":{"device":"/dev/new","filesystem":"btrfs","encrypt":true,"identity":{"device":"/dev/new"}}}`}
	e, err := newEngine(directory, backend)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	s := e.Snapshot()
	if s.Settings == nil || s.Settings.Device != "/dev/new" || s.Settings.Filesystem != "btrfs" || !s.Settings.Encrypt {
		t.Fatalf("stale settings survived backend recovery: %+v", s.Settings)
	}
	if !contains(s.Completed, "install") {
		t.Fatal("deployment metadata not recovered")
	}
}
