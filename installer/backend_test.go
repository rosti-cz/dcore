package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestInstallerCLI(t *testing.T) {
	digest := "sha256:" + strings.Repeat("a", 64)
	tests := []struct {
		args        []string
		mode, image string
		bad         bool
	}{
		{nil, "live", "", false},
		{[]string{"--host"}, "host", "", false},
		{[]string{"install"}, "online", "ghcr.io/rosti-cz/dcore:stable", false},
		{[]string{"install", "--branch", "testing"}, "online", "ghcr.io/rosti-cz/dcore:testing", false},
		{[]string{"install", "--image", "registry.example:5000/team/dcore@" + digest}, "online", "registry.example:5000/team/dcore@" + digest, false},
		{[]string{"install", "--branch", "stable", "--image", "example.com/dcore:tag"}, "", "", true},
		{[]string{"install", "--host"}, "", "", true},
		{[]string{"--branch", "stable"}, "", "", true},
		{[]string{"install", "--branch", "../../oops"}, "", "", true},
		{[]string{"install", "--image", "--privileged"}, "", "", true},
		{[]string{"install", "--image", ""}, "", "", true},
		{[]string{"install", "--image", "oci-archive:/tmp/image"}, "", "", true},
		{[]string{"install", "--image", "docker://example.com/dcore"}, "", "", true},
		{[]string{"install", "--image", "example.com/dcore@sha256:abc"}, "", "", true},
		{[]string{"install", "extra"}, "", "", true},
		{[]string{"--internal-backend", "install"}, "", "", true},
	}
	for _, tt := range tests {
		t.Run(strings.Join(tt.args, " "), func(t *testing.T) {
			o, err := parseOptions(tt.args, &bytes.Buffer{})
			if tt.bad {
				if err == nil {
					t.Fatal("expected argument rejection")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if o.mode != tt.mode || o.image != tt.image {
				t.Fatalf("unexpected options: %+v", o)
			}
		})
	}
	for _, arg := range []string{"--version", "--help"} {
		o, err := parseOptions([]string{arg}, &bytes.Buffer{})
		if err != nil || (!o.version && !o.help) {
			t.Fatalf("%s: %+v %v", arg, o, err)
		}
	}
}
func TestDependencySelection(t *testing.T) {
	present := map[string]bool{"podman": true, "lsblk": true, "findmnt": true}
	lookup := func(tool string) (string, error) {
		if present[tool] {
			return "/bin/" + tool, nil
		}
		return "", errors.New("missing")
	}
	if got := missingTools(false, false, lookup); len(got) != 0 {
		t.Fatal(got)
	}
	if got := missingTools(true, false, lookup); !reflect.DeepEqual(got, []string{"modprobe", "dmsetup", "udevadm"}) {
		t.Fatal(got)
	}
	if got := missingTools(false, true, lookup); !reflect.DeepEqual(got, []string{"mokutil", "openssl"}) {
		t.Fatal(got)
	}
	delete(present, "podman")
	if got := missingTools(false, false, lookup); !reflect.DeepEqual(got, []string{"podman"}) {
		t.Fatal(got)
	}
}

func TestMissingDependenciesHaveActionableError(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	err := dependencyCheck(true, true)
	if err == nil {
		t.Fatal("missing dependencies accepted")
	}
	for _, detail := range []string{"podman", "lsblk", "findmnt", "modprobe", "dmsetup", "udevadm", "mokutil", "openssl", "apt install"} {
		if !strings.Contains(err.Error(), detail) {
			t.Fatalf("dependency error lacks %s: %v", detail, err)
		}
	}
}
func TestPinRegistryReference(t *testing.T) {
	digest := "sha256:" + strings.Repeat("b", 64)
	for _, ref := range []string{"registry.example:5000/team/dcore:stable", "registry.example:5000/team/dcore", "registry.example:5000/team/dcore@" + digest} {
		got, err := pinReference(ref, digest)
		if err != nil || got != "registry.example:5000/team/dcore@"+digest {
			t.Fatalf("%s: %s %v", ref, got, err)
		}
	}
	if _, err := pinReference("example.com/dcore:stable", "unverified"); err == nil {
		t.Fatal("accepted invalid digest")
	}
}
func TestSourceResumePreservesPinnedDigest(t *testing.T) {
	b := &nativeBackend{mode: "online", image: "example.com/dcore:stable", directory: t.TempDir()}
	source := imageSource{Mode: "online", Source: "example.com/dcore@sha256:" + strings.Repeat("c", 64), Local: strings.Repeat("d", 64), Tracking: b.image}
	if err := writeStateJSON(b.directory, "source.json", source); err != nil {
		t.Fatal(err)
	}
	got, err := b.source()
	if err != nil || got != source {
		t.Fatalf("%+v %v", got, err)
	}
	b.image = "example.com/dcore:testing"
	if _, err = b.source(); err == nil {
		t.Fatal("changed the source of an existing session")
	}
	info, err := os.Stat(filepath.Join(b.directory, "source.json"))
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("state permissions: %v %v", info, err)
	}
}
func TestExplicitDigestDoesNotResolveTag(t *testing.T) {
	ref := "example.com/dcore@sha256:" + strings.Repeat("a", 64)
	b := &nativeBackend{mode: "online", image: ref, directory: t.TempDir()}
	t.Setenv("PATH", t.TempDir())
	source, err := b.source()
	if err != nil {
		t.Fatal(err)
	}
	if source.Source != ref || source.Tracking != ref {
		t.Fatalf("digest source not preserved: %+v", source)
	}
}
func fakePodman(t *testing.T, images string) string {
	t.Helper()
	dir := t.TempDir()
	calls := filepath.Join(dir, "calls")
	if err := os.WriteFile(filepath.Join(dir, "images"), []byte(images), 0600); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FIXTURE/calls\"\ncase \"$1\" in\nimages) printf '%s\\n' fixture ;;\nimage) /bin/cat \"$FIXTURE/images\" ;;\n*) exit 90 ;;\nesac\n"
	if err := os.WriteFile(filepath.Join(dir, "podman"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FIXTURE", dir)
	t.Setenv("PATH", dir)
	return calls
}
func TestOfflineCachedImageNeedsNoRegistryOrArchive(t *testing.T) {
	digest := "sha256:" + strings.Repeat("a", 64)
	id := strings.Repeat("d", 64)
	data, _ := json.Marshal([]map[string]any{{"Digest": digest, "Id": id, "Architecture": "amd64", "Labels": map[string]string{"containers.bootc": "1"}}})
	calls := fakePodman(t, string(data))
	b := &nativeBackend{mode: "live", directory: t.TempDir()}
	c := imageSource{Mode: "live", Source: "example.com/dcore@" + digest, Tracking: "example.com/dcore:stable"}
	if err := writeStateJSON(b.directory, "source.json", c); err != nil {
		t.Fatal(err)
	}
	if err := b.load(func(string) {}); err != nil {
		t.Fatal(err)
	}
	if err := b.read("source.json", &c); err != nil {
		t.Fatal(err)
	}
	if c.Local != id {
		t.Fatal(c)
	}
	raw, err := os.ReadFile(calls)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "pull") || strings.Contains(string(raw), "load") {
		t.Fatalf("offline mode contacted registry: %s", raw)
	}
}
func TestCachedImageRejectsWrongDigestAndIncompatibleImage(t *testing.T) {
	digest := "sha256:" + strings.Repeat("a", 64)
	source := imageSource{Source: "example.com/dcore@" + digest, Tracking: "example.com/dcore:stable"}
	for _, tt := range []struct {
		digest, arch, label string
		bad                 bool
	}{
		{"sha256:" + strings.Repeat("b", 64), "amd64", "1", false},
		{digest, "arm64", "1", true},
		{digest, "amd64", "", true},
	} {
		t.Run(tt.digest+tt.arch+tt.label, func(t *testing.T) {
			data, _ := json.Marshal([]map[string]any{{"Digest": tt.digest, "Id": strings.Repeat("c", 64), "Architecture": tt.arch, "Labels": map[string]string{"containers.bootc": tt.label}}})
			fakePodman(t, string(data))
			id, err := (&nativeBackend{}).cached(source)
			if tt.bad {
				if err == nil {
					t.Fatal("incompatible source accepted")
				}
			} else if err != nil || id != "" {
				t.Fatalf("wrong digest accepted: %s %v", id, err)
			}
		})
	}
}
func TestBundledHelpersMatchCanonicalSources(t *testing.T) {
	b := &nativeBackend{directory: t.TempDir()}
	assets, err := b.assets()
	if err != nil {
		t.Fatal(err)
	}
	files, err := filepath.Glob("../scripts/lib/*.py")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range files {
		expected, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		actual, err := os.ReadFile(filepath.Join(assets, filepath.Base(name)))
		if err != nil || !bytes.Equal(expected, actual) {
			t.Fatalf("stale embedded helper %s; run go generate: %v", name, err)
		}
	}
	cert, err := hex.DecodeString(bundledAssets["dcore.der"])
	if err != nil {
		t.Fatal(err)
	}
	expected, err := os.ReadFile("../secureboot/dcore.der")
	if err != nil || !bytes.Equal(cert, expected) {
		t.Fatal("stale bundled certificate")
	}
}
func TestAssetsRejectSymlinks(t *testing.T) {
	dir := t.TempDir()
	victim := filepath.Join(t.TempDir(), "victim")
	if err := os.WriteFile(victim, []byte("untouched"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, filepath.Join(dir, "helper.py")); err != nil {
		t.Fatal(err)
	}
	if err := writeAsset(dir, "helper.py", []byte("changed")); err == nil {
		t.Fatal("followed helper symlink")
	}
	raw, _ := os.ReadFile(victim)
	if string(raw) != "untouched" {
		t.Fatal("overwrote symlink target")
	}
	if err := privateDir(filepath.Join(dir, "helper.py")); err == nil {
		t.Fatal("accepted symlink as private directory")
	}
}
func TestContainerExecutionIsPinnedAndOffline(t *testing.T) {
	b := &nativeBackend{directory: "/private/state"}
	c := imageSource{Local: strings.Repeat("a", 64), Tracking: "example.com/dcore:stable"}
	args := b.container(c, "/private/state/assets", nil, "python3", "helper.py")
	joined := strings.Join(args, " ")
	for _, required := range []string{"--pull=never", "--network=none", "/private/state/assets:/run/dcore-install-lib:ro", "/private/state:/run/dcore-install-state", c.Local} {
		if !strings.Contains(joined, required) {
			t.Fatalf("missing %s: %v", required, args)
		}
	}
	if strings.Contains(joined, c.Tracking) {
		t.Fatal("container runs mutable tag")
	}
}
func TestNativeCredentialProtocol(t *testing.T) {
	input := map[string]any{"core_password": "core-fixture", "luks_passphrase": "disk-fixture"}
	raw, err := credentials(installSettings{Encrypt: true}, input)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err = json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["version"] != float64(1) || decoded["core_password"] != input["core_password"] || decoded["luks_passphrase"] != input["luks_passphrase"] {
		t.Fatal("credential protocol changed")
	}
	for _, bad := range []string{"", "line\nbreak", "nul\x00byte"} {
		if _, err = credentials(installSettings{}, map[string]any{"core_password": bad}); err == nil {
			t.Fatal("accepted malformed credentials")
		}
	}
	if _, err = credentials(installSettings{Encrypt: true}, map[string]any{"core_password": "valid"}); err == nil {
		t.Fatal("accepted missing encryption passphrase")
	}
}
func TestNativeStatusReconcilesWithEngine(t *testing.T) {
	b := &nativeBackend{directory: t.TempDir()}
	settings := installSettings{Device: "/dev/fixture", Filesystem: "ext4", Identity: diskIdentity{Device: "/dev/fixture", Sequence: 10}}
	for name, value := range map[string]any{"settings.json": settings, "layout.json": map[string]any{"deployed": true, "configured": true, "verified": true, "clean": true}} {
		if err := writeStateJSON(b.directory, name, value); err != nil {
			t.Fatal(err)
		}
	}
	interfaceDir := t.TempDir()
	if err := os.Chmod(interfaceDir, 0700); err != nil {
		t.Fatal(err)
	}
	engine, err := newEngine(interfaceDir, b)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.Close()
	snapshot := engine.Snapshot()
	if snapshot.Settings == nil || snapshot.Settings.Device != settings.Device || !contains(snapshot.Completed, "install") || !contains(snapshot.Completed, "verify") {
		t.Fatalf("lost deployed checkpoint: %+v", snapshot)
	}
	if _, err := deviceIdentity("/dev/null", false); err == nil {
		t.Fatal("accepted a character device as install destination")
	}
}

func TestDiskTreeSafety(t *testing.T) {
	noHolders := func(string) (bool, error) { return false, nil }
	disk := blockNode{Name: "/dev/fixture", KName: "fixture", Type: "disk", MajorMinor: "8:16", Size: 10 * gib, Children: []blockNode{{Name: "/dev/fixture1", KName: "fixture1", Type: "part", MajorMinor: "8:17"}}}
	ids, err := inspectDiskTree([]blockNode{disk}, noHolders)
	if err != nil || !ids["8:16"] || !ids["8:17"] {
		t.Fatalf("unused disk refused: %v %v", ids, err)
	}
	for _, kind := range []string{"mounted", "swap", "readonly", "mapping", "holder", "unknown holder state", "partition", "multiple roots"} {
		t.Run(kind, func(t *testing.T) {
			raw, _ := json.Marshal(disk)
			var candidate blockNode
			json.Unmarshal(raw, &candidate)
			nodes := []blockNode{candidate}
			holders := noHolders
			switch kind {
			case "mounted":
				nodes[0].Children[0].Mountpoints = []string{"/home"}
			case "swap":
				nodes[0].Children[0].Mountpoints = []string{"[SWAP]"}
			case "readonly":
				nodes[0].Children[0].RO = true
			case "mapping":
				nodes[0].Children[0].Type = "crypt"
			case "holder":
				holders = func(string) (bool, error) { return true, nil }
			case "unknown holder state":
				holders = func(string) (bool, error) { return false, os.ErrPermission }
			case "partition":
				nodes[0].Type = "part"
			case "multiple roots":
				nodes = append(nodes, candidate)
			}
			if _, err := inspectDiskTree(nodes, holders); err == nil {
				t.Fatal("unsafe destination accepted")
			}
		})
	}
}

func TestHostSourceUsesBootedDigest(t *testing.T) {
	digest := "sha256:" + strings.Repeat("e", 64)
	reference := "registry.example:5000/team/dcore:stable"
	for _, tt := range []struct {
		name  string
		image any
		bad   bool
	}{
		{"nested registry reference", map[string]any{"image": map[string]any{"image": reference, "transport": "registry"}, "imageDigest": digest}, false},
		{"string registry reference", map[string]any{"image": reference, "imageDigest": digest}, false},
		{"missing digest", map[string]any{"image": reference}, true},
		{"wrong transport", map[string]any{"image": map[string]any{"image": reference, "transport": "oci"}, "imageDigest": digest}, true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			raw, _ := json.Marshal(map[string]any{"status": map[string]any{"booted": map[string]any{"image": tt.image}}})
			fixture := filepath.Join(dir, "status.json")
			if err := os.WriteFile(fixture, raw, 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, "bootc"), []byte("#!/bin/sh\n/bin/cat \"$BOOTC_STATUS\"\n"), 0700); err != nil {
				t.Fatal(err)
			}
			t.Setenv("BOOTC_STATUS", fixture)
			t.Setenv("PATH", dir)
			source, err := (&nativeBackend{mode: "host", directory: t.TempDir()}).source()
			if tt.bad {
				if err == nil {
					t.Fatal("unsafe booted source accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if source.Tracking != reference || source.Source != "registry.example:5000/team/dcore@"+digest {
				t.Fatalf("wrong source: %+v", source)
			}
		})
	}
}
