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

func TestTerminalReviewDefaultsToCancelAndRequiresActivation(t *testing.T) {
	b := &fakeBackend{}
	e := testEngine(t, b)
	m, err := newModel(e, nil)
	if err != nil {
		t.Fatal(err)
	}
	m.inputs[0].SetValue("core-secret")
	m.inputs[1].SetValue("core-secret")
	m.focus = 7
	changed, _ := m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = changed.(model)
	if m.review == nil || m.confirmErase || len(b.calls) != 0 {
		t.Fatal("review started installation")
	}
	changed, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = changed.(model)
	if m.review != nil || len(b.calls) != 0 {
		t.Fatal("default did not cancel")
	}
	changed, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = changed.(model)
	changed, _ = m.Update(tea.KeyMsg{Type: tea.KeyRight})
	m = changed.(model)
	changed, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = changed.(model)
	e.wg.Wait()
	if m.review != nil || !e.Snapshot().Done {
		t.Fatal(m.err, e.Snapshot())
	}
}

func TestTerminalPartitionPreviewChangesWithSelectedDrive(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	m, err := newModel(e, nil)
	if err != nil {
		t.Fatal(err)
	}
	m.disks = []Disk{
		{Name: "/dev/first", Children: []Partition{{Name: "/dev/first1", Size: 1 << 30, Filesystem: "ext4", Label: "old-root"}}},
		{Name: "/dev/second"},
	}
	if text := m.View(); !strings.Contains(text, "/dev/first1") || !strings.Contains(text, "old-root") {
		t.Fatal(text)
	}
	changed, _ := m.Update(tea.KeyMsg{Type: tea.KeyRight})
	m = changed.(model)
	if text := m.View(); strings.Contains(text, "/dev/first1") || !strings.Contains(text, "No partitions") {
		t.Fatal(text)
	}
}

func TestTerminalArrowNavigationAndSpaceToggle(t *testing.T) {
	e := testEngine(t, &fakeBackend{})
	m, err := newModel(e, nil)
	if err != nil {
		t.Fatal(err)
	}
	press := func(key tea.KeyType) { changed, _ := m.Update(tea.KeyMsg{Type: key}); m = changed.(model) }
	press(tea.KeyDown)
	if m.focus != 1 || m.filesystem != 0 {
		t.Fatal("Down must select filesystem without changing it")
	}
	press(tea.KeyRight)
	if m.focus != 1 || m.filesystem != 1 {
		t.Fatal("Right must change filesystem")
	}
	press(tea.KeyDown)
	changed, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{' '}})
	m = changed.(model)
	if m.focus != 2 || !m.encrypt {
		t.Fatal("Space must toggle encryption")
	}
	press(tea.KeyDown)
	if m.focus != 3 || !m.inputs[0].Focused() {
		t.Fatal("Down must focus password input")
	}
	press(tea.KeyUp)
	if m.focus != 2 || m.inputs[0].Focused() {
		t.Fatal("Up must blur password input")
	}
	press(tea.KeyShiftTab)
	if m.focus != 1 {
		t.Fatal("Shift+Tab must select previous field")
	}
}
