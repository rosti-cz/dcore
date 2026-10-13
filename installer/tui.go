package main

import (
	"fmt"
	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"os/exec"
	"strings"
	"time"
)

type tickMsg time.Time
type resultMsg struct{ err error }
type diskMsg struct {
	disks []Disk
	err   error
}
type model struct {
	engine       *Engine
	web          *Web
	inputs       []textinput.Model
	disks        []Disk
	selected     int
	filesystem   int
	encrypt      bool
	focus        int
	err          string
	width        int
	height       int
	review       *Input
	confirmErase bool
	enrolling    bool
	logs         bool
	viewport     viewport.Model
}

func newModel(e *Engine, w *Web) (model, error) {
	disks, err := e.Disks()
	if err != nil {
		return model{}, err
	}
	m := model{engine: e, web: w, disks: disks, width: 90, height: 30, viewport: viewport.New(90, 22)}
	for i := 0; i < 4; i++ {
		field := textinput.New()
		field.CharLimit = 4096
		field.Width = 45
		if i < 4 {
			field.EchoMode = textinput.EchoPassword
			field.EchoCharacter = '•'
		}
		m.inputs = append(m.inputs, field)
	}
	s := e.Snapshot()
	if s.Settings != nil {
		for i, d := range disks {
			if d.Name == s.Settings.Device {
				m.selected = i
			}
		}
		for i, f := range []string{"ext4", "btrfs", "xfs"} {
			if f == s.Settings.Filesystem {
				m.filesystem = i
			}
		}
		m.encrypt = s.Settings.Encrypt
	}
	return m, nil
}
func tick() tea.Cmd {
	return tea.Tick(300*time.Millisecond, func(t time.Time) tea.Msg { return tickMsg(t) })
}
func (m model) Init() tea.Cmd { return tick() }
func (m model) input() Input {
	s := m.engine.Snapshot()
	device := ""
	if len(m.disks) > 0 {
		device = m.disks[m.selected].Name
	}
	if s.Settings != nil && contains(s.Completed, "install") {
		device = s.Settings.Device
	}
	return Input{Device: device, Filesystem: []string{"ext4", "btrfs", "xfs"}[m.filesystem], Encrypt: m.encrypt, CorePassword: m.inputs[0].Value(), CoreConfirm: m.inputs[1].Value(), LUKSPassphrase: m.inputs[2].Value(), LUKSConfirm: m.inputs[3].Value(), Revision: s.Revision}
}
func (m model) Update(message tea.Msg) (tea.Model, tea.Cmd) {
	if m.review != nil {
		if key, ok := message.(tea.KeyMsg); ok {
			switch key.String() {
			case "esc":
				m.review = nil
			case "tab", "shift+tab", "left", "right":
				m.confirmErase = !m.confirmErase
			case "enter":
				if !m.confirmErase {
					m.review = nil
					return m, nil
				}
				input := *m.review
				input.Confirmed = true
				m.review = nil
				if err := m.engine.Start(input); err != nil {
					m.err = err.Error()
				} else {
					m.err = ""
					for i := range m.inputs {
						m.inputs[i].Reset()
					}
				}
				return m, nil
			case "ctrl+c":
				m.review = nil
				return m.Update(message)
			}
			return m, nil
		}
	}
	if m.logs {
		if key, ok := message.(tea.KeyMsg); ok && key.String() == "esc" {
			m.logs = false
			return m, nil
		}
		if size, ok := message.(tea.WindowSizeMsg); ok {
			m.width = size.Width
			m.height = size.Height
			m.viewport.Width = size.Width
			m.viewport.Height = size.Height - 4
		}
		if _, ok := message.(tickMsg); ok {
			bottom := m.viewport.AtBottom()
			m.viewport.SetContent(strings.Join(m.engine.Snapshot().Logs, "\n"))
			if bottom {
				m.viewport.GotoBottom()
			}
			return m, tick()
		}
		var cmd tea.Cmd
		m.viewport, cmd = m.viewport.Update(message)
		return m, cmd
	}

	switch msg := message.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
	case tickMsg:
		s := m.engine.Snapshot()
		if s.Settings != nil && (s.Running != "" || contains(s.Completed, "install")) {
			for i, d := range m.disks {
				if d.Name == s.Settings.Device {
					m.selected = i
				}
			}
			for i, f := range []string{"ext4", "btrfs", "xfs"} {
				if f == s.Settings.Filesystem {
					m.filesystem = i
				}
			}
			m.encrypt = s.Settings.Encrypt
		}
		return m, tick()
	case diskMsg:
		if msg.err != nil {
			m.err = msg.err.Error()
		} else {
			m.disks = msg.disks
			m.selected = 0
			m.err = ""
		}
	case resultMsg:
		if msg.err != nil {
			m.err = msg.err.Error()
		} else {
			m.err = ""
		}
	case tea.KeyMsg:
		s := m.engine.Snapshot()
		switch msg.String() {
		case "ctrl+c":
			if s.Running != "" {
				m.err = "Wait for the active operation to finish before exiting."
				return m, nil
			}
			return m, tea.Quit
		case "tab", "shift+tab":
			delta := 1
			if msg.String() == "shift+tab" {
				delta = -1
			}
			m.focus = (m.focus + delta + 13) % 13
			for i := range m.inputs {
				m.inputs[i].Blur()
			}
			if m.focus >= 3 && m.focus <= 6 {
				return m, m.inputs[m.focus-3].Focus()
			}
			return m, nil
		case "left", "right", "up", "down":
			if m.focus < 3 && !contains(s.Completed, "install") && s.Running == "" {
				delta := 1
				if msg.String() == "left" || msg.String() == "up" {
					delta = -1
				}
				switch m.focus {
				case 0:
					if len(m.disks) > 0 {
						m.selected = (m.selected + delta + len(m.disks)) % len(m.disks)
					}
				case 1:
					m.filesystem = (m.filesystem + delta + 3) % 3
				case 2:
					m.encrypt = !m.encrypt
				}
				return m, nil
			}
		case "enter":
			switch m.focus {
			case 7:
				input := m.input()
				m.review = &input
				m.confirmErase = false
				return m, nil
			case 8:
				if s.Running != "" {
					m.err = "Wait for installation before opening a rescue shell."
					return m, nil
				}
				if err := m.engine.BeginRescue(); err != nil {
					m.err = err.Error()
					return m, nil
				}
				return m, tea.ExecProcess(exec.Command("/bin/bash"), func(err error) tea.Msg { m.engine.EndRescue(); return resultMsg{err} })
			case 11:
				if s.Running != "" {
					m.err = "Wait for installation before rescanning drives."
					return m, nil
				}
				return m, func() tea.Msg { disks, err := m.engine.Disks(); return diskMsg{disks, err} }
			case 12:
				m.logs = true
				m.viewport.Width = m.width
				m.viewport.Height = m.height - 4
				m.viewport.SetContent(strings.Join(s.Logs, "\n"))
				m.viewport.GotoBottom()
				return m, nil
			case 10:
				return m, func() tea.Msg { return resultMsg{m.engine.Reboot()} }
			case 9:
				if !s.Done {
					m.err = "Complete installation before enrolling the Secure Boot key."
					return m, nil
				}
				if !m.enrolling {
					m.enrolling = true
					m.inputs[0].Reset()
					m.inputs[1].Reset()
					m.focus = 3
					return m, m.inputs[0].Focus()
				}
				first, second := m.inputs[0].Value(), m.inputs[1].Value()
				m.inputs[0].Reset()
				m.inputs[1].Reset()
				m.enrolling = false
				return m, func() tea.Msg { return resultMsg{m.engine.Enroll(first, second)} }
			}
			if m.enrolling && m.focus == 4 {
				m.focus = 9
				return m, nil
			}
		}
		if m.focus >= 3 && m.focus <= 6 {
			var cmd tea.Cmd
			m.inputs[m.focus-3], cmd = m.inputs[m.focus-3].Update(message)
			return m, cmd
		}
	}
	return m, nil
}
func partitionPreview(parts []Partition, indent string) string {
	if len(parts) == 0 {
		return indent + "No partitions found\n"
	}
	var b strings.Builder
	for _, p := range parts {
		fmt.Fprintf(&b, "%s%s  %.1f GiB  %s  %s  %s\n", indent, p.Name, float64(p.Size)/(1<<30), p.Filesystem, p.Label, strings.Join(p.Mountpoints, ", "))
		if len(p.Children) > 0 {
			b.WriteString(partitionPreview(p.Children, indent+"  "))
		}
	}
	return b.String()
}

func (m model) View() string {
	if m.review != nil {
		input := m.review
		cancel, confirm := "› [ Cancel ]", "  [ Erase drive and install ]"
		if contains(m.engine.Snapshot().Completed, "install") {
			confirm = "  [ Retry failed step ]"
		}
		if m.confirmErase {
			cancel = "  [ Cancel ]"
			confirm = "›" + confirm[1:]
		}
		preview := ""
		for _, d := range m.disks {
			if d.Name == input.Device {
				preview = partitionPreview(d.Children, "  ")
			}
		}
		return fmt.Sprintf("Review installation: %s — %s (encryption: %t)\n\n%s\nAll data on the selected drive will be permanently erased during deployment.\nCompleted phases are preserved when retrying.\n\n%s   %s\n\nTab/Left/Right selects • Enter activates • Esc cancels\n", input.Device, input.Filesystem, input.Encrypt, preview, cancel, confirm)
	}
	if m.logs {
		return "dcore installation log — Esc returns\n\n" + m.viewport.View() + "\nUp/Down and PgUp/PgDown scroll; Home/End select the ends.\n"
	}
	style := lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("75"))
	line := func(index int, label string) string {
		if m.focus == index {
			return style.Render("› " + label)
		}
		return "  " + label
	}
	s := m.engine.Snapshot()
	var b strings.Builder
	b.WriteString(style.Render("dcore installer — terminal and web share this session") + "\n")
	if m.web != nil {
		addresses := m.web.Addresses()
		if len(addresses) == 0 {
			b.WriteString("Web: waiting for a network address")
		} else {
			b.WriteString("Web: " + strings.Join(addresses, "  "))
		}
		b.WriteString("   PIN: " + m.web.PIN() + "\n")
	}
	b.WriteString("Selected drive will be erased. Web access uses HTTP on a trusted network.\n\n")
	device := "No eligible disks"
	if len(m.disks) > 0 {
		d := m.disks[m.selected]
		device = fmt.Sprintf("%s  %.1f GiB  %s  %s", d.Name, float64(d.Size)/(1<<30), d.Model, d.Serial)
	}
	if s.Settings != nil && (s.Running != "" || contains(s.Completed, "install")) {
		device = s.Settings.Device + " (settings locked)"
	}
	b.WriteString(line(0, "Drive: "+device) + "\n")
	if len(m.disks) > 0 {
		b.WriteString(partitionPreview(m.disks[m.selected].Children, "    "))
	}
	b.WriteString(line(1, "Filesystem: "+[]string{"ext4", "btrfs", "xfs"}[m.filesystem]) + "\n")
	encrypt := "none"
	if m.encrypt {
		encrypt = "LUKS2 — console unlock required at boot"
	}
	b.WriteString(line(2, "Encryption: "+encrypt) + "\n")
	labels := []string{"Core password", "Confirm core password", "Disk passphrase", "Confirm disk passphrase"}
	if m.enrolling {
		labels[0] = "Temporary MOK password (1–16 ASCII)"
		labels[1] = "Confirm temporary MOK password"
	}
	for i, field := range m.inputs {
		b.WriteString(line(i+3, labels[i]+": "+field.View()) + "\n")
	}
	start := "Review installation"
	if s.Failed != "" {
		start = "Retry failed step: " + s.Failed
	}
	b.WriteString(line(7, "[ "+start+" ]") + "  " + line(8, "[ Rescue shell ]") + "  " + line(9, "[ Enroll Secure Boot key ]") + "\n")
	b.WriteString(line(11, "[ Rescan drives ]") + "  " + line(12, "[ View logs ]") + "\n")
	if s.CanReboot && s.Done {
		b.WriteString(line(10, "[ Reboot — remove installer media first ]") + "\n")
	}
	if m.enrolling {
		b.WriteString("Enter temporary MOK password and confirmation, then select Enroll key again.\n")
	}
	b.WriteString(fmt.Sprintf("\nProgress: %d%%   Completed: %s   Running: %s\n", len(s.Completed)*100/len(phases), strings.Join(s.Completed, ", "), s.Running))
	if s.Done {
		b.WriteString(style.Render("Installation complete. Remove installer media and reboot. MOK enrollment is confirmed in firmware.") + "\n")
	}
	if s.Error != "" {
		b.WriteString("Step error: " + s.Error + "\n")
	}
	if m.err != "" {
		b.WriteString("Error: " + m.err + "\n")
	}
	n := m.height - 23
	if n < 2 {
		n = 2
	}
	if n > len(s.Logs) {
		n = len(s.Logs)
	}
	if n > 0 {
		b.WriteString(strings.Join(s.Logs[len(s.Logs)-n:], "\n") + "\n")
	}
	b.WriteString("Tab selects fields • arrows change choices • Enter activates • Ctrl+C exits when idle\n")
	return b.String()
}
