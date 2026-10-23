package main

import (
	"bufio"
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

const gib = uint64(1024 * 1024 * 1024)
const medium = "/run/live/medium"

type imageSource struct {
	Mode       string `json:"mode"`
	Source     string `json:"source_image"`
	Local      string `json:"local_image"`
	Tracking   string `json:"tracking_image"`
	Release    string `json:"release"`
	Debian     string `json:"debian_release"`
	Size       uint64 `json:"image_size_bytes"`
	MinimumRAM uint64 `json:"minimum_ram_bytes"`
}
type diskIdentity struct {
	Device     string `json:"device"`
	MajorMinor string `json:"major_minor"`
	Sequence   uint64 `json:"diskseq"`
	Size       uint64 `json:"size"`
}
type installSettings struct {
	Device     string       `json:"device"`
	Identity   diskIdentity `json:"identity"`
	Filesystem string       `json:"filesystem"`
	Encrypt    bool         `json:"encrypt"`
}
type nativeBackend struct{ mode, image, directory string }

func privateDir(path string) error {
	if err := os.MkdirAll(path, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	st := info.Sys().(*syscall.Stat_t)
	if !info.IsDir() || info.Mode().Perm()&0077 != 0 || st.Uid != uint32(os.Geteuid()) {
		return fmt.Errorf("state directory must be private and owned by the caller: %s", path)
	}
	return nil
}
func writeStateJSON(dir, name string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".state-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(raw); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(f.Name(), filepath.Join(dir, name)); err != nil {
		return err
	}
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
func (b *nativeBackend) read(name string, value any) error {
	raw, err := os.ReadFile(filepath.Join(b.directory, name))
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, value)
}
func outputCommand(name string, args ...string) ([]byte, error) {
	cmd := exec.Command(name, args...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("%s failed: %s (%w)", name, strings.TrimSpace(stderr.String()), err)
	}
	return out, nil
}
func logCommand(input []byte, log func(string), name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdin = bytes.NewReader(input)
	out, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = cmd.Stdout
	if err = cmd.Start(); err != nil {
		return err
	}
	// Reuse the bounded line handling used by the engine's command backend.
	scanner := bufio.NewScanner(out)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	for scanner.Scan() {
		log(scanner.Text())
	}
	if scanner.Err() != nil {
		io.Copy(io.Discard, out)
	}
	err = cmd.Wait()
	if scanner.Err() != nil {
		return scanner.Err()
	}
	if err != nil {
		return fmt.Errorf("%s failed; inspect the log and retry", name)
	}
	return nil
}

func missingTools(encrypt, enroll bool, lookup func(string) (string, error)) []string {
	tools := []string{"podman", "lsblk", "findmnt"}
	if encrypt {
		tools = append(tools, "modprobe", "dmsetup", "udevadm")
	}
	if enroll {
		tools = append(tools, "mokutil", "openssl")
	}
	var missing []string
	for _, tool := range tools {
		if _, err := lookup(tool); err != nil {
			missing = append(missing, tool)
		}
	}
	return missing
}
func dependencyCheck(encrypt, enroll bool) error {
	missing := missingTools(encrypt, enroll, exec.LookPath)
	if len(missing) > 0 {
		return fmt.Errorf("missing required commands: %s. Install Podman (including its OCI runtime), util-linux, and ca-certificates; encryption also needs kmod, dmsetup and udev; key enrollment needs mokutil and openssl. On Debian/Ubuntu: apt install podman util-linux ca-certificates%s%s", strings.Join(missing, ", "), map[bool]string{true: " kmod dmsetup udev"}[encrypt], map[bool]string{true: " mokutil openssl"}[enroll])
	}
	if runtime.GOOS != "linux" || runtime.GOARCH != "amd64" {
		return errors.New("installation requires Linux amd64")
	}
	if os.Geteuid() != 0 {
		return errors.New("installation backend requires root")
	}
	if _, err := os.Stat("/sys/firmware/efi"); err != nil {
		return errors.New("installation requires a host booted in UEFI mode")
	}
	for _, name := range []string{"/.dockerenv", "/run/.containerenv", "/run/systemd/container"} {
		if _, err := os.Stat(name); err == nil {
			return errors.New("run on the Linux host, outside containers")
		}
	}
	self, err := os.Readlink("/proc/self/ns/mnt")
	if err != nil {
		return err
	}
	init, err := os.Readlink("/proc/1/ns/mnt")
	if err != nil {
		return err
	}
	if self != init {
		return errors.New("run in the host mount namespace")
	}
	if encrypt {
		if _, err := os.Stat("/usr/lib/udev/rules.d/95-dm-notify.rules"); err != nil {
			return errors.New("encryption requires device-mapper udev notification rules (95-dm-notify.rules)")
		}
		if _, err := outputCommand("modprobe", "--quiet", "dm-crypt"); err != nil {
			return fmt.Errorf("host kernel cannot load dm-crypt: %w", err)
		}
	}
	return nil
}
func (b *nativeBackend) preflight(encrypt bool) error {
	if err := dependencyCheck(encrypt, false); err != nil {
		return err
	}
	out, err := outputCommand("podman", "info", "--format", "{{.Store.GraphRoot}}")
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(out)) != "/var/lib/containers/storage" {
		return errors.New("installer currently requires rootful Podman graphroot /var/lib/containers/storage")
	}
	if err := os.MkdirAll("/var/lib/containers", 0700); err != nil {
		return err
	}
	return nil
}

func ensureKernelFilesystem(filesystem string) error {
	available := func() bool {
		raw, err := os.ReadFile("/proc/filesystems")
		if err != nil {
			return false
		}
		for _, line := range strings.Split(string(raw), "\n") {
			fields := strings.Fields(line)
			if len(fields) > 0 && fields[len(fields)-1] == filesystem {
				return true
			}
		}
		return false
	}
	if available() {
		return nil
	}
	if _, err := exec.LookPath("modprobe"); err != nil {
		return fmt.Errorf("host kernel lacks %s support; install kmod and the matching kernel modules", filesystem)
	}
	if _, err := outputCommand("modprobe", "--quiet", filesystem); err != nil {
		return fmt.Errorf("cannot load host kernel filesystem %s: %w", filesystem, err)
	}
	if !available() {
		return fmt.Errorf("host kernel does not support %s", filesystem)
	}
	return nil
}

func operationLock() (func(), error) {
	fd, err := unix.Open("/run/dcore-install-go-operation.lock", unix.O_CREAT|unix.O_RDWR|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if err != nil {
		return nil, err
	}
	if err = unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB); err != nil {
		unix.Close(fd)
		return nil, errors.New("another installation operation is running")
	}
	return func() { unix.Close(fd) }, nil
}
func freeSpace(path string) (uint64, error) {
	var s unix.Statfs_t
	err := unix.Statfs(path, &s)
	return s.Bavail * uint64(s.Bsize), err
}
func (b *nativeBackend) liveResources(c imageSource, requireFree bool) error {
	raw, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return err
	}
	memory := map[string]uint64{}
	for _, line := range strings.Split(string(raw), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 1 {
			n, _ := strconv.ParseUint(fields[1], 10, 64)
			memory[strings.TrimSuffix(fields[0], ":")] = n * 1024
		}
	}
	minimum := c.MinimumRAM
	if minimum == 0 {
		minimum = 16 * gib
	}
	if memory["MemTotal"]+gib < minimum {
		return fmt.Errorf("offline installer requires at least %d GiB RAM", minimum/gib)
	}
	out, err := outputCommand("findmnt", "--noheadings", "--output", "FSTYPE", "--target", "/var/lib/containers")
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(out)) != "tmpfs" {
		return errors.New("offline image staging requires its dedicated RAM filesystem")
	}
	temp := "/var/lib/containers/image-tmp"
	if err := privateDir(temp); err != nil {
		return err
	}
	if err := os.Setenv("TMPDIR", temp); err != nil {
		return err
	}
	if requireFree {
		size := c.Size
		if size == 0 {
			size = 4 * gib
		}
		needed := 2*size + gib
		free, err := freeSpace("/var/lib/containers")
		if err != nil {
			return err
		}
		if free < needed || memory["MemAvailable"] < needed {
			return fmt.Errorf("offline image loading requires %d GiB free RAM staging space", (needed+gib-1)/gib)
		}
	}
	return nil
}
func pinReference(reference, digest string) (string, error) {
	if err := validateImage(reference); err != nil {
		return "", err
	}
	if !regexp.MustCompile(`^sha256:[a-f0-9]{64}$`).MatchString(digest) {
		return "", errors.New("image has no valid immutable sha256 digest")
	}
	repo := strings.Split(reference, "@")[0]
	if strings.LastIndex(repo, ":") > strings.LastIndex(repo, "/") {
		repo = repo[:strings.LastIndex(repo, ":")]
	}
	return repo + "@" + digest, nil
}
func (b *nativeBackend) source() (imageSource, error) {
	var c imageSource
	if err := b.read("source.json", &c); err == nil {
		if b.mode == "online" && c.Tracking != b.image {
			return c, errors.New("this session belongs to another image; finish it before changing the source")
		}
		return c, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return c, err
	}
	c.Mode = b.mode
	switch b.mode {
	case "live":
		raw, err := os.ReadFile(medium + "/dcore/installer.json")
		if err != nil {
			return c, err
		}
		if err = json.Unmarshal(raw, &c); err != nil {
			return c, err
		}
		c.Mode = "live"
		if !strings.Contains(c.Source, "@sha256:") {
			return c, errors.New("offline source must be pinned by digest")
		}
		if err := validateImage(c.Source); err != nil {
			return c, err
		}
		if err := b.liveResources(c, false); err != nil {
			return c, err
		}
	case "host":
		raw, err := outputCommand("bootc", "status", "--format=json")
		if err != nil {
			return c, err
		}
		var status struct {
			Status struct {
				Booted *struct {
					Image struct {
						Image  json.RawMessage `json:"image"`
						Digest string          `json:"imageDigest"`
					} `json:"image"`
				} `json:"booted"`
			} `json:"status"`
		}
		if err = json.Unmarshal(raw, &status); err != nil {
			return c, err
		}
		if status.Status.Booted == nil {
			return c, errors.New("no booted bootc image")
		}
		image := status.Status.Booted.Image
		if err = json.Unmarshal(image.Image, &c.Tracking); err != nil {
			var ref struct{ Image, Transport string }
			if err = json.Unmarshal(image.Image, &ref); err != nil {
				return c, err
			}
			if ref.Transport != "" && ref.Transport != "registry" {
				return c, errors.New("booted source must use registry transport")
			}
			c.Tracking = ref.Image
		}
		c.Source, err = pinReference(c.Tracking, image.Digest)
		if err != nil {
			return c, err
		}
		c.Local = c.Source
	case "online":
		c.Tracking = b.image
		if strings.Contains(b.image, "@") {
			c.Source = b.image
			c.Local = b.image
		} else {
			free, err := freeSpace("/var/lib/containers")
			if err != nil {
				return c, err
			}
			if free < 10*gib {
				return c, errors.New("image staging requires at least 10 GiB free at /var/lib/containers")
			}
			fmt.Fprintln(os.Stderr, "Resolving and downloading", b.image)
			if err := logCommand(nil, func(line string) { fmt.Fprintln(os.Stderr, line) }, "podman", "pull", b.image); err != nil {
				return c, err
			}
			out, err := outputCommand("podman", "image", "inspect", "--format", "{{.Digest}}", b.image)
			if err != nil {
				return c, err
			}
			c.Source, err = pinReference(b.image, strings.TrimSpace(string(out)))
			if err != nil {
				return c, err
			}
			c.Local = c.Source
		}
	default:
		return c, errors.New("invalid installer mode")
	}
	return c, writeStateJSON(b.directory, "source.json", c)
}
func (b *nativeBackend) cached(c imageSource) (string, error) {
	out, err := outputCommand("podman", "images", "--no-trunc", "--format", "{{.ID}}")
	if err != nil {
		return "", err
	}
	names := append([]string{c.Source, c.Tracking}, strings.Fields(string(out))...)
	expected := strings.Split(c.Source, "@")
	if len(expected) != 2 {
		return "", errors.New("source is not pinned")
	}
	for _, name := range names {
		raw, err := outputCommand("podman", "image", "inspect", name)
		if err != nil {
			continue
		}
		var images []struct {
			Digest       string
			ID           string
			Architecture string
			Labels       map[string]string
		}
		if json.Unmarshal(raw, &images) != nil {
			continue
		}
		for _, image := range images {
			if image.Digest == expected[1] {
				if image.Architecture != "amd64" || image.Labels["containers.bootc"] != "1" {
					return "", errors.New("source must be an amd64 bootc image (containers.bootc=1)")
				}
				if !regexp.MustCompile(`^(sha256:)?[a-f0-9]{64}$`).MatchString(image.ID) {
					return "", errors.New("invalid cached image ID")
				}
				return image.ID, nil
			}
		}
	}
	return "", nil
}
func (b *nativeBackend) load(log func(string)) error {
	c, err := b.source()
	if err != nil {
		return err
	}
	id, err := b.cached(c)
	if err != nil {
		return err
	}
	if id == "" {
		if b.mode == "live" {
			if err = b.liveResources(c, true); err != nil {
				return err
			}
			err = logCommand(nil, log, "podman", "load", "--input", medium+"/dcore/image.oci.tar.gz")
		} else {
			free, e := freeSpace("/var/lib/containers")
			if e != nil {
				return e
			}
			if free < 10*gib {
				return errors.New("image staging requires at least 10 GiB free at /var/lib/containers")
			}
			err = logCommand(nil, log, "podman", "pull", c.Source)
		}
		if err != nil {
			return err
		}
		id, err = b.cached(c)
		if err != nil {
			return err
		}
	}
	if id == "" {
		return errors.New("cached image digest does not match the selected source")
	}
	c.Local = id
	return writeStateJSON(b.directory, "source.json", c)
}

type blockNode struct {
	Name        string      `json:"name"`
	KName       string      `json:"kname"`
	Type        string      `json:"type"`
	Size        uint64      `json:"size"`
	RO          bool        `json:"ro"`
	MajorMinor  string      `json:"maj:min"`
	Mountpoints []string    `json:"mountpoints"`
	Children    []blockNode `json:"children"`
}

func inspectDiskTree(nodes []blockNode, hasHolders func(string) (bool, error)) (map[string]bool, error) {
	if len(nodes) != 1 || nodes[0].Type != "disk" {
		return nil, errors.New("destination must be a whole physical disk")
	}
	identifiers := map[string]bool{}
	var walk func(blockNode) error
	walk = func(n blockNode) error {
		identifiers[n.MajorMinor] = true
		if n.RO {
			return errors.New("destination contains a read-only device")
		}
		for _, m := range n.Mountpoints {
			if m != "" {
				return errors.New("destination contains a mounted filesystem or active swap")
			}
		}
		if n.Type != "disk" && n.Type != "part" {
			return errors.New("destination contains an active storage mapping")
		}
		held, err := hasHolders(n.KName)
		if err != nil {
			return err
		}
		if held {
			return errors.New("destination has active device holders")
		}
		for _, child := range n.Children {
			if err := walk(child); err != nil {
				return err
			}
		}
		return nil
	}
	if err := walk(nodes[0]); err != nil {
		return nil, err
	}
	return identifiers, nil
}

func deviceIdentity(device string, cleanup bool) (diskIdentity, error) {
	var id diskIdentity
	resolved, err := filepath.EvalSymlinks(device)
	if err != nil {
		return id, err
	}
	id.Device = resolved
	var st unix.Stat_t
	if err = unix.Stat(device, &st); err != nil {
		return id, err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFBLK {
		return id, errors.New("destination must be a block device")
	}
	id.MajorMinor = fmt.Sprintf("%d:%d", unix.Major(st.Rdev), unix.Minor(st.Rdev))
	sys := "/sys/dev/block/" + id.MajorMinor
	if _, err = os.Stat(sys + "/partition"); err == nil {
		return id, errors.New("choose a whole disk, not a partition")
	}
	raw, err := os.ReadFile(sys + "/diskseq")
	if err != nil {
		return id, err
	}
	id.Sequence, err = strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return id, err
	}
	if cleanup {
		return id, nil
	}
	self, err := os.Readlink("/proc/self/ns/mnt")
	if err != nil {
		return id, err
	}
	init, err := os.Readlink("/proc/1/ns/mnt")
	if err != nil {
		return id, err
	}
	if self != init {
		return id, errors.New("run in the host mount namespace")
	}
	raw, err = outputCommand("lsblk", "--json", "--paths", "--bytes", "--output", "NAME,KNAME,TYPE,SIZE,RO,MOUNTPOINTS,MAJ:MIN", device)
	if err != nil {
		return id, err
	}
	var listing struct {
		Devices []blockNode `json:"blockdevices"`
	}
	if err = json.Unmarshal(raw, &listing); err != nil {
		return id, err
	}
	identifiers, err := inspectDiskTree(listing.Devices, func(kname string) (bool, error) {
		holders, err := os.ReadDir("/sys/class/block/" + filepath.Base(kname) + "/holders")
		return len(holders) > 0, err
	})
	if err != nil {
		return id, err
	}
	swaps, err := os.ReadFile("/proc/swaps")
	if err != nil {
		return id, err
	}
	for i, line := range strings.Split(string(swaps), "\n") {
		if i == 0 {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		name := regexp.MustCompile(`\\([0-7]{3})`).ReplaceAllStringFunc(fields[0], func(s string) string { v, _ := strconv.ParseUint(s[1:], 8, 8); return string(byte(v)) })
		var swap unix.Stat_t
		if err = unix.Stat(name, &swap); err != nil {
			return id, err
		}
		if swap.Mode&unix.S_IFMT == unix.S_IFBLK && identifiers[fmt.Sprintf("%d:%d", unix.Major(swap.Rdev), unix.Minor(swap.Rdev))] {
			return id, errors.New("destination has active swap")
		}
	}
	fd, err := unix.Open(device, unix.O_RDONLY|unix.O_EXCL|unix.O_CLOEXEC, 0)
	if err != nil {
		return id, errors.New("destination is busy, mounted, swap or mapped")
	}
	unix.Close(fd)
	id.Size = listing.Devices[0].Size
	if id.Size < 10*gib {
		return id, errors.New("destination requires at least 10 GiB")
	}
	return id, nil
}
func mediaDevices() (map[string]bool, error) {
	out, err := outputCommand("findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", medium)
	if err != nil {
		return nil, err
	}
	source := strings.TrimSpace(string(out))
	var st unix.Stat_t
	if err = unix.Stat(source, &st); err != nil {
		return nil, err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFBLK {
		return nil, errors.New("cannot identify offline installer medium")
	}
	out, err = outputCommand("lsblk", "--inverse", "--noheadings", "--paths", "--output", "NAME", source)
	if err != nil {
		return nil, err
	}
	result := map[string]bool{}
	for _, name := range strings.Fields(string(out)) {
		result[name] = true
	}
	return result, nil
}
func (b *nativeBackend) identity(s installSettings, cleanup bool) error {
	if b.mode == "live" {
		excluded, err := mediaDevices()
		if err != nil {
			return err
		}
		if excluded[s.Device] || excluded[s.Identity.Device] {
			return errors.New("cannot install onto the offline installer medium")
		}
	}
	actual, err := deviceIdentity(s.Device, cleanup)
	if err != nil {
		return err
	}
	expected := s.Identity
	if cleanup {
		expected.Size = 0
	}
	if actual != expected {
		return errors.New("destination identity or size changed; refusing to continue")
	}
	return nil
}
func (b *nativeBackend) disks() (any, error) {
	excluded := map[string]bool{}
	var err error
	if b.mode == "live" {
		excluded, err = mediaDevices()
		if err != nil {
			return nil, err
		}
	}
	raw, err := outputCommand("lsblk", "--json", "--paths", "--bytes", "--output", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,LABEL,MOUNTPOINTS")
	if err != nil {
		return nil, err
	}
	var listing struct {
		Devices []json.RawMessage `json:"blockdevices"`
	}
	if err = json.Unmarshal(raw, &listing); err != nil {
		return nil, err
	}
	rows := [][2]any{}
	for _, raw := range listing.Devices {
		var node struct{ Name, Type string }
		if json.Unmarshal(raw, &node) != nil || node.Type != "disk" || excluded[node.Name] {
			continue
		}
		id, err := deviceIdentity(node.Name, false)
		if err == nil {
			rows = append(rows, [2]any{raw, id})
		}
	}
	return rows, nil
}
func (b *nativeBackend) Query(operation string, value, result any) error {
	answer, err := b.query(operation, value)
	if err != nil {
		return err
	}
	if result != nil {
		raw, err := json.Marshal(answer)
		if err != nil {
			return err
		}
		return json.Unmarshal(raw, result)
	}
	return nil
}
func (b *nativeBackend) query(operation string, value any) (any, error) {
	if operation == "source" || operation == "settings" || operation == "reboot" {
		unlock, err := operationLock()
		if err != nil {
			return nil, err
		}
		defer unlock()
	}
	switch operation {
	case "source":
		return b.source()
	case "status":
		var layout, settings any
		if err := b.read("layout.json", &layout); err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		if err := b.read("settings.json", &settings); err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		return map[string]any{"layout": layout, "settings": settings}, nil
	case "disks":
		return b.disks()
	case "settings":
		raw, err := json.Marshal(value)
		if err != nil {
			return nil, err
		}
		var s installSettings
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err = decoder.Decode(&s); err != nil {
			return nil, err
		}
		if s.Filesystem != "ext4" && s.Filesystem != "btrfs" && s.Filesystem != "xfs" {
			return nil, errors.New("invalid filesystem")
		}
		if _, err = os.Stat(filepath.Join(b.directory, "layout.json")); !errors.Is(err, os.ErrNotExist) {
			return nil, errors.New("an OS is already deployed; resume its remaining stages")
		}
		if err = b.preflight(s.Encrypt); err != nil {
			return nil, err
		}
		if err = ensureKernelFilesystem(s.Filesystem); err != nil {
			return nil, err
		}
		if err = b.identity(s, false); err != nil {
			return nil, err
		}
		return nil, writeStateJSON(b.directory, "settings.json", s)
	case "reboot":
		if _, err := exec.LookPath("systemctl"); err != nil {
			return nil, errors.New("reboot requires systemctl; reboot the live environment manually")
		}
		var layout map[string]any
		if err := b.read("layout.json", &layout); err != nil {
			return nil, err
		}
		if b.mode != "live" || !completeLayout(layout) {
			return nil, errors.New("reboot is available only after completed offline installation")
		}
		_, err := outputCommand("systemctl", "reboot")
		return nil, err
	}
	return nil, fmt.Errorf("unknown backend operation %s", operation)
}
func completeLayout(layout map[string]any) bool {
	for _, key := range []string{"deployed", "configured", "verified", "clean"} {
		if layout[key] != true {
			return false
		}
	}
	return true
}
func (b *nativeBackend) assets() (string, error) {
	dir := filepath.Join(b.directory, "assets")
	if err := privateDir(dir); err != nil {
		return "", err
	}
	for name, data := range bundledAssets {
		if name == "dcore.der" {
			continue
		}
		if err := writeAsset(dir, name, []byte(data)); err != nil {
			return "", err
		}
	}
	return dir, nil
}
func writeAsset(dir, name string, data []byte) error {
	path := filepath.Join(dir, name)
	fd, err := unix.Open(path, unix.O_WRONLY|unix.O_CREAT|unix.O_TRUNC|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if err != nil {
		return err
	}
	f := os.NewFile(uintptr(fd), path)
	_, err = f.Write(data)
	closeErr := f.Close()
	if err != nil {
		return err
	}
	return closeErr
}
func (b *nativeBackend) container(c imageSource, assets string, extra []string, command ...string) []string {
	args := []string{"run", "--rm", "--pull=never", "--network=none", "-i", "--privileged", "--pid=host", "--ipc=host", "--security-opt", "label=disable", "-v", "/dev:/dev", "-v", "/var/lib/containers:/var/lib/containers", "-v", b.directory + ":/run/dcore-install-state", "-v", assets + ":/run/dcore-install-lib:ro"}
	args = append(args, extra...)
	args = append(args, c.Local)
	return append(args, command...)
}
func credentials(s installSettings, value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var request struct {
		Core string `json:"core_password"`
		LUKS string `json:"luks_passphrase"`
	}
	if err = json.Unmarshal(raw, &request); err != nil {
		return nil, err
	}
	if err = password(request.Core); err != nil {
		return nil, err
	}
	if !s.Encrypt {
		return []byte(request.Core), nil
	}
	if err = password(request.LUKS); err != nil {
		return nil, err
	}
	return json.Marshal(map[string]any{"version": 1, "core_password": request.Core, "luks_passphrase": request.LUKS})
}
func (b *nativeBackend) Phase(operation string, value any, log func(string)) error {
	// Serialize privileged operations across host, online and ISO sessions.
	unlock, err := operationLock()
	if err != nil {
		return err
	}
	defer unlock()
	if operation == "enroll" {
		return b.enroll(value, log)
	}
	var s installSettings
	if err = b.read("settings.json", &s); err != nil {
		return err
	}
	if err = b.preflight(s.Encrypt); err != nil {
		return err
	}
	if operation == "validate" || operation == "install" {
		if err = ensureKernelFilesystem(s.Filesystem); err != nil {
			return err
		}
	}
	if operation == "load" {
		return b.load(log)
	}
	c, err := b.source()
	if err != nil {
		return err
	}
	assets, err := b.assets()
	if err != nil {
		return err
	}
	if err = b.identity(s, operation == "cleanup"); err != nil {
		return err
	}
	if operation == "validate" || operation == "install" {
		command := []string{"python3", "/run/dcore-install-lib/preflight-install.py", "--filesystem", s.Filesystem}
		if s.Encrypt {
			command = append(command, "--encrypt")
		}
		if err = logCommand(nil, log, "podman", b.container(c, assets, nil, command...)...); err != nil {
			return err
		}
		if operation == "validate" {
			return nil
		}
	}
	if operation == "install" {
		var request map[string]any
		raw, _ := json.Marshal(value)
		if err = json.Unmarshal(raw, &request); err != nil {
			return err
		}
		if request["confirmed"] != true {
			return errors.New("fresh erase confirmation is required")
		}
		if _, err = os.Stat(filepath.Join(b.directory, "layout.json")); !errors.Is(err, os.ErrNotExist) {
			return errors.New("an OS is already deployed; refusing another erase")
		}
		payload, err := credentials(s, value)
		if err != nil {
			return err
		}
		raw, err = outputCommand("podman", "image", "inspect", "--format", "{{.Size}}", c.Local)
		if err != nil {
			return err
		}
		size, err := strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 64)
		if err != nil {
			return err
		}
		free, err := freeSpace("/var/lib/containers")
		if err != nil {
			return err
		}
		if free < size+gib {
			return errors.New("insufficient staging space; destination unchanged")
		}
		// Recheck after preparation, immediately before crossing mount namespaces.
		if err = b.identity(s, false); err != nil {
			return err
		}
		var command []string
		if s.Encrypt {
			command = []string{"python3", "/run/dcore-install-lib/install-encrypted.py", "--inside", "--device", s.Device, "--filesystem", s.Filesystem, "--image", c.Local, "--target-image", c.Tracking, "--local", "--core-password-stdin", "--defer-account", "--state-dir", "/run/dcore-install-state"}
		} else {
			command = []string{"python3", "/run/dcore-install-lib/install-password.py", s.Device, "--defer-account", "--state-dir", "/run/dcore-install-state", "--filesystem", s.Filesystem, "--composefs-backend", "--bootloader", "systemd", "--disable-selinux", "--generic-image", "--target-imgref", c.Tracking, "--skip-fetch-check", "--karg", "console=tty0"}
			if s.Filesystem == "btrfs" {
				command = append(command, "--karg", "rootflags=compress=zstd:1")
			}
			if s.Filesystem == "xfs" {
				command = append(command, "--allow-missing-verity")
			}
		}
		err = logCommand(payload, log, "podman", b.container(c, assets, nil, command...)...)
		unix.Sync()
		return err
	}
	if operation != "configure" && operation != "verify" && operation != "cleanup" {
		return errors.New("unknown installation operation")
	}
	var payload []byte
	if operation != "cleanup" {
		payload, err = credentials(s, value)
		if err != nil {
			return err
		}
	}
	return logCommand(payload, log, "podman", b.container(c, assets, nil, "python3", "/run/dcore-install-lib/finish-install.py", operation, "--state-dir", "/run/dcore-install-state")...)
}
func (b *nativeBackend) enroll(value any, log func(string)) error {
	if err := dependencyCheck(false, true); err != nil {
		return err
	}
	var layout map[string]any
	if err := b.read("layout.json", &layout); err != nil {
		return err
	}
	if !completeLayout(layout) {
		return errors.New("complete installation before requesting enrollment")
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	var request struct {
		Password string `json:"enrollment_password"`
		Confirm  string `json:"enrollment_confirm"`
	}
	if err = json.Unmarshal(raw, &request); err != nil {
		return err
	}
	if len(request.Password) < 1 || len(request.Password) > 16 || request.Password != request.Confirm {
		return errors.New("enrollment passwords must match and contain 1–16 printable ASCII characters")
	}
	for _, r := range request.Password {
		if r < 32 || r > 126 {
			return errors.New("invalid enrollment password")
		}
	}
	cert, err := hex.DecodeString(bundledAssets["dcore.der"])
	if err != nil {
		return err
	}
	if err = writeAsset(b.directory, "dcore.der", cert); err != nil {
		return err
	}
	path := filepath.Join(b.directory, "dcore.der")
	cmd := exec.Command("mokutil", "--test-key", path)
	cmd.Env = append(os.Environ(), "LC_ALL=C")
	out, status := cmd.CombinedOutput()
	message := string(out)
	for _, s := range []string{" is already enrolled", " is already in db", " is already in the built-in trusted keyring", " is already in the enrollment request"} {
		if strings.Contains(message, s) {
			log("The certificate is already enrolled or enrollment is pending.")
			return nil
		}
	}
	var exit *exec.ExitError
	if status != nil && (!errors.As(status, &exit) || exit.ExitCode() != 1) {
		return errors.New("cannot determine certificate enrollment status")
	}
	if !strings.Contains(message, " is not enrolled") {
		return errors.New("cannot determine certificate enrollment status")
	}
	hashCmd := exec.Command("openssl", "passwd", "-6", "-stdin")
	hashCmd.Stdin = strings.NewReader(request.Password + "\n")
	hash, err := hashCmd.Output()
	if err != nil {
		return errors.New("cannot hash enrollment password")
	}
	if !bytes.HasPrefix(hash, []byte("$6$")) || len(hash) > 256 {
		return errors.New("invalid enrollment hash")
	}
	fd, err := unix.MemfdCreate("dcore-mok-password", unix.MFD_CLOEXEC)
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fd), "mok-password")
	defer file.Close()
	if _, err = file.Write(hash); err != nil {
		return err
	}
	if _, err = file.Seek(0, 0); err != nil {
		return err
	}
	cmd = exec.Command("mokutil", "--import", path, "--hash-file", "/proc/self/fd/3")
	cmd.ExtraFiles = []*os.File{file}
	cmd.Env = append(os.Environ(), "LC_ALL=C")
	out, err = cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("enrollment failed: %s", out)
	}
	log("Enrollment requested. Reboot and confirm in MokManager.")
	return nil
}
