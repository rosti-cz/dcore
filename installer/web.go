package main

import (
	"crypto/rand"
	"crypto/subtle"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

//go:embed web.html
var webFiles embed.FS

type webSession struct {
	csrf    string
	expires time.Time
}

type loginAttempts struct {
	count int
	until time.Time
}

type Web struct {
	engine         *Engine
	pin            string
	addresses      []string
	hosts          map[string]bool
	mu             sync.Mutex
	sessions       map[string]webSession
	attempts       map[string]loginAttempts
	globalAttempts loginAttempts
	now            func() time.Time
	addressSource  func() ([]net.IP, error)
	refreshedAt    time.Time
}

func randomToken() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}

func NewWeb(engine *Engine) (*Web, error) {
	n, err := rand.Int(rand.Reader, big.NewInt(1000000))
	if err != nil {
		return nil, err
	}
	w := &Web{engine: engine, pin: fmt.Sprintf("%06d", n), sessions: make(map[string]webSession), attempts: make(map[string]loginAttempts), now: time.Now, addressSource: interfaceIPs}
	w.refreshAddresses()
	return w, nil
}

func interfaceIPs() ([]net.IP, error) {
	interfaces, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	var ips []net.IP
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ip, _, err := net.ParseCIDR(addr.String())
			if err != nil || ip.IsUnspecified() || ip.IsMulticast() {
				continue
			}
			ips = append(ips, ip)
		}
	}
	return ips, nil
}

// Called with mu held. DHCP may assign an address after the installer starts.
func (w *Web) refreshAddresses() {
	now := w.now()
	if !w.refreshedAt.IsZero() && now.Sub(w.refreshedAt) < 2*time.Second {
		return
	}
	w.refreshedAt = now
	w.hosts = map[string]bool{"localhost": true, "127.0.0.1": true, "::1": true}
	w.addresses = nil
	ips, err := w.addressSource()
	if err != nil {
		return
	} // Fail closed instead of retaining stale interface addresses.
	seen := make(map[string]bool)
	for _, ip := range ips {
		if ip.IsUnspecified() || ip.IsMulticast() {
			continue
		}
		w.hosts[ip.String()] = true
		if ip.IsLoopback() || ip.IsLinkLocalUnicast() {
			continue
		}
		host := ip.String()
		if ip.To4() == nil {
			host = "[" + host + "]"
		}
		address := "http://" + host + "/"
		if !seen[address] {
			w.addresses = append(w.addresses, address)
			seen[address] = true
		}
	}
	sort.Strings(w.addresses)
}

func (w *Web) PIN() string { return w.pin }
func (w *Web) Addresses() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.refreshAddresses()
	return append([]string(nil), w.addresses...)
}

func (w *Web) validHost(host string) bool {
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	host = strings.Trim(host, "[]")
	w.mu.Lock()
	defer w.mu.Unlock()
	w.refreshAddresses()
	return w.hosts[host]
}

func writeJSON(rw http.ResponseWriter, status int, value any) {
	rw.Header().Set("Content-Type", "application/json")
	rw.WriteHeader(status)
	_ = json.NewEncoder(rw).Encode(value)
}

func webError(rw http.ResponseWriter, status int, message string) {
	writeJSON(rw, status, map[string]string{"error": message})
}

func decodeForm(rw http.ResponseWriter, r *http.Request, value any) error {
	if strings.Split(r.Header.Get("Content-Type"), ";")[0] != "application/json" {
		return errors.New("Expected a JSON request")
	}
	r.Body = http.MaxBytesReader(rw, r.Body, 128*1024)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(value); err != nil {
		return errors.New("Invalid request")
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return errors.New("Invalid request")
	}
	return nil
}

func (w *Web) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/login", w.login)
	mux.HandleFunc("/api/state", w.state)
	mux.HandleFunc("/api/start", w.start)
	mux.HandleFunc("/api/enroll", w.enroll)
	mux.HandleFunc("/api/reboot", w.reboot)
	mux.HandleFunc("/", func(rw http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(rw, r)
			return
		}
		if r.Method != http.MethodGet {
			rw.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		page, _ := webFiles.ReadFile("web.html")
		rw.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = rw.Write(page)
	})
	return http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		rw.Header().Set("Cache-Control", "no-store")
		rw.Header().Set("X-Content-Type-Options", "nosniff")
		rw.Header().Set("X-Frame-Options", "DENY")
		rw.Header().Set("Referrer-Policy", "no-referrer")
		rw.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
		if !w.validHost(r.Host) {
			webError(rw, http.StatusForbidden, "Use the IP address printed by the installer")
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" {
			u, err := url.Parse(origin)
			if err != nil || u.Scheme != "http" || u.Host != r.Host {
				webError(rw, http.StatusForbidden, "Invalid request origin")
				return
			}
		}
		if r.Header.Get("Sec-Fetch-Site") == "cross-site" {
			webError(rw, http.StatusForbidden, "Cross-site requests are not allowed")
			return
		}
		mux.ServeHTTP(rw, r)
	})
}

func (w *Web) login(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		rw.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var form struct {
		PIN string `json:"pin"`
	}
	if err := decodeForm(rw, r, &form); err != nil {
		webError(rw, 400, err.Error())
		return
	}
	peer, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		peer = r.RemoteAddr
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	now := w.now()
	for ip, attempts := range w.attempts {
		if !now.Before(attempts.until) {
			delete(w.attempts, ip)
		}
	}
	for token, session := range w.sessions {
		if !now.Before(session.expires) {
			delete(w.sessions, token)
		}
	}
	a := w.attempts[peer]
	if !now.Before(w.globalAttempts.until) {
		w.globalAttempts = loginAttempts{}
	}
	if w.globalAttempts.count >= 50 {
		webError(rw, 429, "Too many attempts. Try again in five minutes.")
		return
	}
	if a.count >= 5 && now.Before(a.until) {
		webError(rw, 429, "Too many attempts. Try again in five minutes.")
		return
	}
	if len(form.PIN) != 6 || subtle.ConstantTimeCompare([]byte(form.PIN), []byte(w.pin)) != 1 {
		w.globalAttempts.count++
		w.globalAttempts.until = now.Add(5 * time.Minute)
		a.count++
		a.until = now.Add(5 * time.Minute)
		if len(w.attempts) >= 1024 {
			webError(rw, 429, "Too many attempts. Try again later.")
			return
		}
		w.attempts[peer] = a
		webError(rw, 401, "Incorrect PIN")
		return
	}
	if len(w.sessions) >= 64 {
		webError(rw, 429, "Too many active sessions. Try again later.")
		return
	}
	token, err := randomToken()
	if err != nil {
		webError(rw, 500, "Could not create session")
		return
	}
	csrf, err := randomToken()
	if err != nil {
		webError(rw, 500, "Could not create session")
		return
	}
	w.sessions[token] = webSession{csrf: csrf, expires: now.Add(2 * time.Hour)}
	delete(w.attempts, peer)
	http.SetCookie(rw, &http.Cookie{Name: "dcore_session", Value: token, Path: "/", HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: 7200})
	writeJSON(rw, 200, map[string]string{"csrf": csrf})
}

func (w *Web) session(rw http.ResponseWriter, r *http.Request, mutation bool) (webSession, bool) {
	cookie, err := r.Cookie("dcore_session")
	if err != nil {
		webError(rw, 401, "Enter the installer PIN")
		return webSession{}, false
	}
	w.mu.Lock()
	s, ok := w.sessions[cookie.Value]
	if ok && !w.now().Before(s.expires) {
		delete(w.sessions, cookie.Value)
		ok = false
	}
	w.mu.Unlock()
	if !ok {
		webError(rw, 401, "Session expired. Enter the installer PIN again")
		return webSession{}, false
	}
	if mutation && subtle.ConstantTimeCompare([]byte(r.Header.Get("X-CSRF-Token")), []byte(s.csrf)) != 1 {
		webError(rw, 403, "Invalid session token")
		return webSession{}, false
	}
	return s, true
}

func (w *Web) state(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		rw.WriteHeader(405)
		return
	}
	session, ok := w.session(rw, r, false)
	if !ok {
		return
	}
	disks, err := w.engine.Disks()
	if err != nil {
		webError(rw, 500, err.Error())
		return
	}
	writeJSON(rw, 200, map[string]any{"snapshot": w.engine.Snapshot(), "disks": disks, "csrf": session.csrf})
}

func (w *Web) start(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		rw.WriteHeader(405)
		return
	}
	if _, ok := w.session(rw, r, true); !ok {
		return
	}
	var form Input
	if err := decodeForm(rw, r, &form); err != nil {
		webError(rw, 400, err.Error())
		return
	}
	if err := w.engine.Start(form); err != nil {
		webError(rw, 409, err.Error())
		return
	}
	writeJSON(rw, 202, map[string]bool{"started": true})
}

func (w *Web) enroll(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		rw.WriteHeader(405)
		return
	}
	if _, ok := w.session(rw, r, true); !ok {
		return
	}
	var form struct {
		Password string `json:"password"`
		Confirm  string `json:"confirm"`
	}
	if err := decodeForm(rw, r, &form); err != nil {
		webError(rw, 400, err.Error())
		return
	}
	if err := w.engine.Enroll(form.Password, form.Confirm); err != nil {
		webError(rw, 409, err.Error())
		return
	}
	writeJSON(rw, 200, map[string]bool{"requested": true})
}

func (w *Web) reboot(rw http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		rw.WriteHeader(405)
		return
	}
	if _, ok := w.session(rw, r, true); !ok {
		return
	}
	var form struct {
		Confirmation string `json:"confirmation"`
	}
	if err := decodeForm(rw, r, &form); err != nil {
		webError(rw, 400, err.Error())
		return
	}
	if form.Confirmation != "REBOOT" {
		webError(rw, 400, "Confirm reboot first")
		return
	}
	if err := w.engine.Reboot(); err != nil {
		webError(rw, 409, err.Error())
		return
	}
	writeJSON(rw, 202, map[string]bool{"rebooting": true})
}
