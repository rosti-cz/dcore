package main

import (
	tea "github.com/charmbracelet/bubbletea"
	"strings"
	"testing"
)

func TestTerminalTracksBrowserCommittedSettings(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	m, err := newModel(e, nil)
	if err != nil {
		t.Fatal(err)
	}
	e.mu.Lock()
	e.state.Settings = &Settings{Device: "/dev/fixture", Filesystem: "xfs", Encrypt: true}
	e.state.Running = "install"
	e.mu.Unlock()
	changed, _ := m.Update(tickMsg{})
	m = changed.(model)
	if m.filesystem != 2 || !m.encrypt {
		t.Fatal("terminal did not follow browser settings")
	}
	if !strings.Contains(m.View(), "settings locked") {
		t.Fatal(m.View())
	}
}
func TestTerminalSecretsAreMaskedAndQuitBlockedWhileBusy(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	m, err := newModel(e, nil)
	if err != nil {
		t.Fatal(err)
	}
	m.inputs[0].SetValue("not-for-display")
	if strings.Contains(m.View(), "not-for-display") {
		t.Fatal("password rendered")
	}
	e.mu.Lock()
	e.state.Running = "install"
	e.mu.Unlock()
	changed, cmd := m.Update(tea.KeyMsg{Type: tea.KeyCtrlC})
	if cmd != nil || changed.(model).err == "" {
		t.Fatal("allowed exit during installation")
	}
}
