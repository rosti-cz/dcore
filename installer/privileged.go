package main

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"golang.org/x/sys/unix"
)

func openNativeBackend(mode, image string, uid int) (*nativeBackend, error) {
	root := "/run/dcore-install-go"
	if err := privateDir(root); err != nil {
		return nil, err
	}
	directory := filepath.Join(root, mode)
	if mode == "online" {
		if err := privateDir(directory); err != nil {
			return nil, err
		}
		directory = filepath.Join(directory, fmt.Sprintf("%x", sha256.Sum256([]byte(image))))
	}
	if mode == "host" {
		directory += "-" + strconv.Itoa(uid)
	}
	if err := privateDir(directory); err != nil {
		return nil, err
	}
	if mode == "live" {
		if _, err := os.Stat(medium + "/dcore/installer.json"); err != nil {
			return nil, errors.New("offline mode requires the booted dcore installer ISO; use 'install' on other hosts")
		}
	}
	return &nativeBackend{mode: mode, image: image, directory: directory}, nil
}

func internalBackend(operation string) error {
	if os.Geteuid() != 0 {
		return errors.New("internal backend requires root")
	}
	uid, err := strconv.Atoi(os.Getenv("SUDO_UID"))
	if err != nil || uid <= 0 {
		return errors.New("internal host backend must be launched through sudo by an ordinary user")
	}
	os.Setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
	unix.Setrlimit(unix.RLIMIT_CORE, &unix.Rlimit{})
	if operation == "bind-web" {
		return bindHostWeb(uid)
	}
	b, err := openNativeBackend("host", "", uid)
	if err != nil {
		return err
	}
	if operation == "source" {
		if err = b.preflight(false); err != nil {
			return err
		}
	}
	var request any
	if operation != "source" && operation != "status" && operation != "disks" {
		raw, err := io.ReadAll(io.LimitReader(os.Stdin, 65537))
		if err != nil {
			return err
		}
		if len(raw) > 65536 {
			return errors.New("oversized installer request")
		}
		if len(raw) == 0 {
			raw = []byte("{}")
		}
		if err = json.Unmarshal(raw, &request); err != nil {
			return errors.New("invalid installer request")
		}
	}
	switch operation {
	case "source", "status", "disks", "settings", "reboot":
		result, err := b.query(operation, request)
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(result)
	case "load", "validate", "install", "configure", "verify", "cleanup", "enroll":
		return b.Phase(operation, request, func(line string) { fmt.Println(line) })
	default:
		return errors.New("unknown internal operation")
	}
}
func bindHostWeb(uid int) error {
	directory := fmt.Sprintf("/run/user/%d/dcore-installer", uid)
	for _, path := range []string{filepath.Dir(directory), directory} {
		var st unix.Stat_t
		if err := unix.Lstat(path, &st); err != nil {
			return err
		}
		if st.Mode&unix.S_IFMT != unix.S_IFDIR || st.Uid != uint32(uid) || st.Mode&0077 != 0 {
			return errors.New("web listener directory must be private and owned by the caller")
		}
	}
	path := filepath.Join(directory, "listener.sock")
	var st unix.Stat_t
	if err := unix.Lstat(path, &st); err != nil {
		return err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFSOCK || st.Uid != uint32(uid) {
		return errors.New("invalid web listener socket")
	}
	peer, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return err
	}
	defer peer.Close()
	peer.SetDeadline(time.Now().Add(10 * time.Second))
	raw, err := peer.SyscallConn()
	if err != nil {
		return err
	}
	var credential *unix.Ucred
	var checkErr error
	if err = raw.Control(func(fd uintptr) {
		credential, checkErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	}); err != nil {
		return err
	}
	if checkErr != nil || credential == nil || credential.Uid != uint32(uid) {
		return errors.New("web listener peer does not match caller")
	}
	listener, err := net.Listen("tcp", ":80")
	if err != nil {
		return err
	}
	defer listener.Close()
	file, err := listener.(*net.TCPListener).File()
	if err != nil {
		return err
	}
	defer file.Close()
	_, _, err = peer.WriteMsgUnix([]byte("dcore-http"), unix.UnixRights(int(file.Fd())), nil)
	return err
}
