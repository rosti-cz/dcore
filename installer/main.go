package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/x/term"
	"golang.org/x/sys/unix"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func webListener(privileged bool, directory string) (net.Listener, error) {
	if privileged {
		return net.Listen("tcp", ":80")
	}
	socket := filepath.Join(directory, "listener.sock")
	if err := os.Remove(socket); err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	addr := &net.UnixAddr{Name: socket, Net: "unix"}
	listener, err := net.ListenUnix("unix", addr)
	if err != nil {
		return nil, err
	}
	defer listener.Close()
	if err = os.Chmod(socket, 0600); err != nil {
		return nil, err
	}
	listener.SetDeadline(time.Now().Add(10 * time.Second))
	cmd := commandBackend{}.command("bind-web")
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err = cmd.Start(); err != nil {
		return nil, err
	}
	finished := make(chan error, 1)
	go func() { finished <- cmd.Wait() }()
	conn, err := listener.AcceptUnix()
	if err != nil {
		if cmd.Process != nil {
			cmd.Process.Kill()
		}
		<-finished
		return nil, fmt.Errorf("web listener: %s (%s)", err, output.String())
	}
	defer conn.Close()
	raw, err := conn.SyscallConn()
	if err != nil {
		return nil, err
	}
	var credential *unix.Ucred
	var checkErr error
	raw.Control(func(fd uintptr) {
		credential, checkErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	})
	if checkErr != nil || credential == nil || credential.Uid != 0 {
		return nil, errors.New("web binder peer must be root")
	}
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	data := make([]byte, 1)
	ancillary := make([]byte, unix.CmsgSpace(4))
	_, n, _, _, err := conn.ReadMsgUnix(data, ancillary)
	if err != nil {
		<-finished
		if detail := strings.TrimSpace(output.String()); detail != "" {
			return nil, fmt.Errorf("port 80 listener: %s", detail)
		}
		return nil, err
	}
	messages, err := unix.ParseSocketControlMessage(ancillary[:n])
	if err != nil {
		return nil, err
	}
	fds := []int{}
	for _, message := range messages {
		rights, err := unix.ParseUnixRights(&message)
		if err != nil {
			return nil, err
		}
		fds = append(fds, rights...)
	}
	defer func() {
		for _, fd := range fds {
			unix.Close(fd)
		}
	}()
	if len(fds) != 1 {
		return nil, errors.New("web binder did not return exactly one socket")
	}
	file := os.NewFile(uintptr(fds[0]), "web-listener")
	fds = nil
	defer file.Close()
	result, err := net.FileListener(file)
	if helperErr := <-finished; helperErr != nil {
		if result != nil {
			result.Close()
		}
		return nil, fmt.Errorf("web binder: %s", output.String())
	}
	return result, err
}
func runMain() error {
	o, err := parseOptions(os.Args[1:], os.Stdout)
	if err != nil {
		return err
	}
	if o.help {
		return nil
	}
	if o.version {
		fmt.Printf("dcore installer %s (Go / Bubble Tea)\n", installerVersion)
		return nil
	}
	if o.operation != "" {
		return internalBackend(o.operation)
	}
	host := o.mode == "host"
	live := o.mode == "live"
	if live {
		if _, err := os.Stat(medium + "/dcore/installer.json"); errors.Is(err, os.ErrNotExist) {
			_, err := parseOptions([]string{"--help"}, os.Stdout)
			return err
		}
	}
	if !host && os.Geteuid() != 0 {
		return errors.New("run the installer as root (sudo dcore-installer install)")
	}
	if host && os.Geteuid() == 0 {
		return errors.New("run djust install as an ordinary user, without sudo")
	}
	if !term.IsTerminal(os.Stdin.Fd()) {
		return errors.New("installer requires an interactive terminal")
	}
	unix.Setrlimit(unix.RLIMIT_CORE, &unix.Rlimit{})
	directory := "/run/dcore-installer-" + o.mode
	var backend Backend
	if host {
		if err := exec.Command("/usr/bin/sudo", "-v").Run(); err != nil {
			return err
		}
		directory = "/run/user/" + strconv.Itoa(os.Geteuid()) + "/dcore-installer"
		backend = commandBackend{}
	} else {
		b, err := openNativeBackend(o.mode, o.image, 0)
		if err != nil {
			return err
		}
		if err = b.preflight(false); err != nil {
			return err
		}
		if o.mode == "online" {
			directory = filepath.Join(b.directory, "interface")
		}
		backend = b
	}
	var source imageSource
	if err := backend.Query("source", nil, &source); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "Installation source: %s (updates: %s)\n", source.Source, source.Tracking)
	engine, err := newEngine(directory, backend)
	if err != nil {
		return err
	}
	defer engine.Close()
	engine.source = source
	engine.state.CanReboot = live
	web, err := NewWeb(engine)
	if err != nil {
		return err
	}
	listener, err := webListener(!host, directory)
	var server *http.Server
	webError := ""
	if err != nil {
		webError = "Web installer unavailable: " + err.Error()
		fmt.Fprintln(os.Stderr, webError)
		web = nil
	} else {
		server = &http.Server{Handler: web.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 20 * time.Second, WriteTimeout: 20 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 8192}
		go server.Serve(listener)
		defer func() {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			server.Shutdown(ctx)
		}()
	}
	m, err := newModel(engine, web)
	if err != nil {
		return err
	}
	if webError != "" {
		m.err = webError
	}
	_, err = tea.NewProgram(m, tea.WithAltScreen()).Run()
	return err
}
func main() {
	if err := runMain(); err != nil {
		fmt.Fprintln(os.Stderr, "dcore installer:", err)
		os.Exit(1)
	}
}
