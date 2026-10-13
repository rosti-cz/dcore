package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func newTestWeb(t *testing.T) *Web {
	t.Helper()
	w, err := NewWeb(&Engine{})
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func requestWeb(w *Web, method, path, body string, cookie *http.Cookie, csrf string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "http://127.0.0.1"+path, strings.NewReader(body))
	r.RemoteAddr = "192.0.2.10:12345"
	r.Header.Set("Content-Type", "application/json")
	if cookie != nil {
		r.AddCookie(cookie)
	}
	if csrf != "" {
		r.Header.Set("X-CSRF-Token", csrf)
	}
	rw := httptest.NewRecorder()
	w.Handler().ServeHTTP(rw, r)
	return rw
}

func loginWeb(t *testing.T, w *Web) (*http.Cookie, string) {
	t.Helper()
	rw := requestWeb(w, "POST", "/api/login", `{"pin":"`+w.PIN()+`"}`, nil, "")
	if rw.Code != 200 {
		t.Fatalf("login: %d %s", rw.Code, rw.Body.String())
	}
	var data map[string]string
	if err := json.Unmarshal(rw.Body.Bytes(), &data); err != nil {
		t.Fatal(err)
	}
	cookies := rw.Result().Cookies()
	if len(cookies) != 1 || !cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteStrictMode {
		t.Fatal("session cookie lacks protections")
	}
	if len(data["csrf"]) != 64 {
		t.Fatal("missing random CSRF token")
	}
	return cookies[0], data["csrf"]
}

func TestWebAccessRequiresPINAndCSRF(t *testing.T) {
	w := newTestWeb(t)
	if len(w.PIN()) != 6 {
		t.Fatal("PIN must have six digits")
	}
	if rw := requestWeb(w, "GET", "/api/state", "", nil, ""); rw.Code != 401 {
		t.Fatal("state exposed without authentication")
	}
	cookie, csrf := loginWeb(t, w)
	for _, path := range []string{"/api/start", "/api/enroll", "/api/reboot"} {
		if rw := requestWeb(w, "POST", path, "{}", cookie, ""); rw.Code != 403 {
			t.Fatalf("missing CSRF accepted for %s", path)
		}
	}
	// Invalid input reaches the engine only after the session token is verified.
	if rw := requestWeb(w, "POST", "/api/enroll", `{"password":"one","confirm":"two"}`, cookie, csrf); rw.Code != 409 {
		t.Fatalf("expected validation failure, got %d", rw.Code)
	}
}

func TestWebAddressesFollowDHCPAndRemoveStaleHosts(t *testing.T) {
	w := newTestWeb(t)
	now := time.Now()
	w.now = func() time.Time { return now }
	w.refreshedAt = time.Time{}
	var ips []net.IP
	w.addressSource = func() ([]net.IP, error) { return ips, nil }
	if len(w.Addresses()) != 0 {
		t.Fatal("addresses should be empty before DHCP")
	}
	ips = []net.IP{net.ParseIP("192.0.2.15"), net.ParseIP("2001:db8::15")}
	now = now.Add(3 * time.Second)
	if got := w.Addresses(); len(got) != 2 || got[0] != "http://192.0.2.15/" || got[1] != "http://[2001:db8::15]/" {
		t.Fatalf("DHCP addresses missing: %v", got)
	}
	if !w.validHost("192.0.2.15") || !w.validHost("[2001:db8::15]:80") {
		t.Fatal("assigned addresses rejected")
	}
	ips = []net.IP{net.ParseIP("192.0.2.25")}
	now = now.Add(3 * time.Second)
	if w.validHost("192.0.2.15") || !w.validHost("192.0.2.25") {
		t.Fatal("host allowlist did not follow lease change")
	}
}

func TestWebRebootRequiresExplicitConfirmation(t *testing.T) {
	w := newTestWeb(t)
	cookie, csrf := loginWeb(t, w)
	if rw := requestWeb(w, "POST", "/api/reboot", `{"confirmation":""}`, cookie, csrf); rw.Code != 400 {
		t.Fatal("reboot accepted without confirmation")
	}
}

func TestWebPINLockoutAndExpiry(t *testing.T) {
	w := newTestWeb(t)
	now := time.Now()
	w.now = func() time.Time { return now }
	wrong := "000000"
	if wrong == w.PIN() {
		wrong = "000001"
	}
	for i := 0; i < 5; i++ {
		if rw := requestWeb(w, "POST", "/api/login", `{"pin":"`+wrong+`"}`, nil, ""); rw.Code != 401 {
			t.Fatalf("attempt %d: %d", i, rw.Code)
		}
	}
	if rw := requestWeb(w, "POST", "/api/login", `{"pin":"`+w.PIN()+`"}`, nil, ""); rw.Code != 429 {
		t.Fatal("lockout was bypassed")
	}
	now = now.Add(5 * time.Minute)
	cookie, _ := loginWeb(t, w)
	now = now.Add(2 * time.Hour)
	if rw := requestWeb(w, "POST", "/api/enroll", "{}", cookie, ""); rw.Code != 401 {
		t.Fatal("expired cookie accepted")
	}
}

func TestWebPINHasGlobalDistributedAttemptLimit(t *testing.T) {
	w := newTestWeb(t)
	wrong := "000000"
	if wrong == w.PIN() {
		wrong = "000001"
	}
	for i := 0; i < 50; i++ {
		r := httptest.NewRequest("POST", "http://127.0.0.1/api/login", strings.NewReader(`{"pin":"`+wrong+`"}`))
		r.RemoteAddr = fmt.Sprintf("192.0.2.%d:12345", i+1)
		r.Header.Set("Content-Type", "application/json")
		rw := httptest.NewRecorder()
		w.Handler().ServeHTTP(rw, r)
		if rw.Code != 401 {
			t.Fatalf("attempt %d: %d", i, rw.Code)
		}
	}
	if rw := requestWeb(w, "POST", "/api/login", `{"pin":"`+w.PIN()+`"}`, nil, ""); rw.Code != 429 {
		t.Fatal("distributed PIN guessing bypassed global rate limit")
	}
}

func TestWebRejectsRebindingAndCrossOrigin(t *testing.T) {
	w := newTestWeb(t)
	for _, test := range []struct{ host, origin, fetch string }{{"attacker.example", "", ""}, {"127.0.0.1", "http://attacker.example", ""}, {"127.0.0.1", "http://127.0.0.1", "cross-site"}, {"127.0.0.1", "https://127.0.0.1", ""}} {
		r := httptest.NewRequest("GET", "http://127.0.0.1/", nil)
		r.Host = test.host
		r.Header.Set("Origin", test.origin)
		r.Header.Set("Sec-Fetch-Site", test.fetch)
		rw := httptest.NewRecorder()
		w.Handler().ServeHTTP(rw, r)
		if rw.Code != 403 {
			t.Fatalf("unsafe request accepted: %+v", test)
		}
	}
	if rw := requestWeb(w, "GET", "/", "", nil, ""); rw.Code != 200 || !strings.Contains(rw.Body.String(), "dcore installer") {
		t.Fatal("offline page unavailable")
	}
}

func TestWebRejectsOversizedUnknownAndTrailingJSON(t *testing.T) {
	w := newTestWeb(t)
	for _, body := range []string{`{"pin":"123456","unexpected":true}`, `{"pin":"123456"} {}`, `{"pin":"` + strings.Repeat("0", 131072) + `"}`} {
		if rw := requestWeb(w, "POST", "/api/login", body, nil, ""); rw.Code != 400 {
			t.Fatalf("invalid JSON accepted: %d", rw.Code)
		}
	}
}

func TestWebSessionTokensAreIndependent(t *testing.T) {
	w := newTestWeb(t)
	a, ac := loginWeb(t, w)
	b, bc := loginWeb(t, w)
	if a.Value == b.Value || ac == bc {
		t.Fatal("session tokens reused")
	}
	if rw := requestWeb(w, "POST", "/api/enroll", "{}", a, bc); rw.Code != 403 {
		t.Fatal("another session CSRF accepted")
	}
}

type webBackend struct{}

func (webBackend) Query(operation string, _ any, result any) error {
	if result == nil {
		return nil
	}
	raw := `{ "layout": {} }`
	if operation == "disks" {
		raw = `[[{"name":"/dev/test","size":10737418240,"model":"Test disk"},{"device":"/dev/test"}]]`
	}
	return json.Unmarshal([]byte(raw), result)
}

func (webBackend) Phase(_ string, _ any, log func(string)) error {
	log("Example log: core-secret disk-secret")
	return nil
}

func TestWebInstallationUsesSharedEngineWithoutExposingSecrets(t *testing.T) {
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	e, err := newEngine(directory, webBackend{})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	w, err := NewWeb(e)
	if err != nil {
		t.Fatal(err)
	}
	cookie, csrf := loginWeb(t, w)
	state := requestWeb(w, "GET", "/api/state", "", cookie, "")
	if state.Code != 200 || !strings.Contains(state.Body.String(), "/dev/test") {
		t.Fatal("disk list unavailable")
	}
	input := Input{Device: "/dev/test", Filesystem: "btrfs", Encrypt: true, CorePassword: "core-secret", CoreConfirm: "core-secret", LUKSPassphrase: "disk-secret", LUKSConfirm: "disk-secret", Confirmed: true, Revision: e.Snapshot().Revision}
	raw, _ := json.Marshal(input)
	rw := requestWeb(w, "POST", "/api/start", string(raw), cookie, csrf)
	if rw.Code != 202 {
		t.Fatalf("start failed: %d %s", rw.Code, rw.Body.String())
	}
	e.wg.Wait()
	state = requestWeb(w, "GET", "/api/state", "", cookie, "")
	if state.Code != 200 || !strings.Contains(state.Body.String(), `"done":true`) {
		t.Fatalf("shared progress unavailable: %s", state.Body.String())
	}
	if strings.Contains(state.Body.String(), "core-secret") || strings.Contains(state.Body.String(), "disk-secret") {
		t.Fatal("credentials exposed in browser state")
	}
	if rw = requestWeb(w, "POST", "/api/start", string(raw), cookie, csrf); rw.Code != 409 {
		t.Fatal("duplicate start accepted")
	}
}
