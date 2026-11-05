package xray

// Tests against the real, pinned Xray binary (decision D10: no imitation of Xray). They run when
// PROXYHUB_TEST_XRAY names the binary that node-image/fetch-xray.sh downloaded (geoip.dat next to
// it); both files must match the sha256 values in node-image/versions.lock. Everything happens on
// loopback: a REALITY server rendered by Render, a REALITY client, a local TLS 1.3 + h2 target; and
// a VLESS + WebSocket + TLS server rendered by Render with a throwaway CA's certificate, and a real
// client that verifies that certificate (the ws_tls link's exact shape).

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdh"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/testcert"
)

func lockValue(t *testing.T, key string) string {
	t.Helper()
	data, err := os.ReadFile("../../../node-image/versions.lock")
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(string(data), "\n") {
		if v, ok := strings.CutPrefix(line, key+"="); ok {
			return v
		}
	}
	t.Fatalf("versions.lock has no %s", key)
	return ""
}

func fileSHA256(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// pinnedXray returns the pinned binary and its assets directory, or skips.
func pinnedXray(t *testing.T) (string, string) {
	t.Helper()
	bin := os.Getenv("PROXYHUB_TEST_XRAY")
	if bin == "" {
		t.Skip("PROXYHUB_TEST_XRAY not set (node-image/fetch-xray.sh DIR, then DIR/xray)")
	}
	if got := fileSHA256(t, bin); got != lockValue(t, "XCORE_BINARY_SHA256") {
		t.Fatalf("PROXYHUB_TEST_XRAY is not the pinned Xray build (sha256 %s)", got)
	}
	assets := filepath.Dir(bin)
	if got := fileSHA256(t, filepath.Join(assets, "geoip.dat")); got != lockValue(t, "XCORE_GEOIP_SHA256") {
		t.Fatal("geoip.dat next to the binary is not the pinned one")
	}
	return bin, assets
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

func x25519Pair(t *testing.T) (priv, pub string) {
	t.Helper()
	key, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(key.Bytes()), base64.RawURLEncoding.EncodeToString(key.PublicKey().Bytes())
}

// lockedBuffer collects a process's output (test only: on a node Xray's output goes to /dev/null).
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func startXray(t *testing.T, bin, assets string, config []byte) *lockedBuffer {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, config, 0o600); err != nil {
		t.Fatal(err)
	}
	out := &lockedBuffer{}
	cmd := exec.Command(bin, "run", "-config", path)
	cmd.Env = []string{"XRAY_LOCATION_ASSET=" + assets}
	cmd.Stdout, cmd.Stderr = out, out
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})
	return out
}

func waitFor(t *testing.T, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if ok() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestRealXrayAcceptsTheRenderedConfig(t *testing.T) {
	bin, assets := pinnedXray(t)
	priv, _ := x25519Pair(t)
	x := testDesired()
	x.PrivateKey = priv
	path := filepath.Join(t.TempDir(), "config.json")
	if err := WriteConfig(path, render(t, x), fsutil.KeepOwner); err != nil {
		t.Fatal(err)
	}
	svc := Service{Binary: bin, AssetsDir: assets}
	if err := svc.Test(context.Background(), path); err != nil {
		t.Fatalf("the pinned Xray refuses the rendered config: %v", err)
	}
	// No user left (every config ended): Xray still starts, with nobody admitted.
	empty, err := Render(x, nil, "127.0.0.1:10085", TLSFiles{})
	if err != nil {
		t.Fatal(err)
	}
	if err := WriteConfig(path, empty, fsutil.KeepOwner); err != nil {
		t.Fatal(err)
	}
	if err := svc.Test(context.Background(), path); err != nil {
		t.Fatalf("the pinned Xray refuses a config without users: %v", err)
	}
	// Negative control: the same binary refuses a broken config (the test is real).
	var m map[string]any
	_ = json.Unmarshal(render(t, x), &m)
	m["inbounds"].([]any)[0].(map[string]any)["protocol"] = "no-such-protocol"
	broken, _ := json.Marshal(m)
	if err := os.WriteFile(path, broken, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := svc.Test(context.Background(), path); err == nil {
		t.Fatal("xray run -test accepted a broken config")
	}
	// Without geoip.dat the private-range block can not load: the assets directory matters.
	if err := (Service{Binary: bin, AssetsDir: t.TempDir()}).Test(context.Background(), path); err == nil {
		t.Fatal("config accepted without geoip.dat")
	}
}

var ipv4Like = regexp.MustCompile(`(?:^|[^0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:[^0-9.]|$)`)

// localPrivateIPv4 is a non-loopback IPv4 address of this machine (a private one on any test
// machine or container), or "" if there is none.
func localPrivateIPv4() string {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return ""
	}
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil && !n.IP.IsLoopback() && n.IP.IsPrivate() {
			return n.IP.String()
		}
	}
	return ""
}

// realityLab is a loopback REALITY setup: a local TLS 1.3 + h2 target site, a plain HTTP service
// on this machine's private LAN address (and loopback) that counts its hits, and one user.
type realityLab struct {
	bin, assets string
	target      *httptest.Server
	hits        atomic.Int64
	lan         string
	privatePort int
	user        User
	x           *desired.Xray
	pub         string
	apiPort     int
}

func newRealityLab(t *testing.T) *realityLab {
	t.Helper()
	bin, assets := pinnedXray(t)
	lab := &realityLab{bin: bin, assets: assets, lan: localPrivateIPv4()}
	if lab.lan == "" {
		t.Skip("this machine has no private IPv4 address to test the private-range block with")
	}
	lab.target = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "target site")
	}))
	lab.target.Config.ErrorLog = log.New(io.Discard, "", 0)
	lab.target.EnableHTTP2 = true
	lab.target.StartTLS()
	t.Cleanup(lab.target.Close)

	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	private := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		lab.hits.Add(1)
		_, _ = io.WriteString(w, "private")
	}))
	private.Listener = listener
	private.Start()
	t.Cleanup(private.Close)
	lab.privatePort = listener.Addr().(*net.TCPAddr).Port
	// Control: the service answers on the LAN address directly, so zero hits through the proxy
	// below means the proxy blocked it, not that it was unreachable anyway.
	resp, err := http.Get(fmt.Sprintf("http://%s:%d/", lab.lan, lab.privatePort))
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("control request to the private service failed: %v", err)
	}
	_ = resp.Body.Close()
	lab.hits.Store(0)

	priv, pub := x25519Pair(t)
	lab.pub, lab.apiPort = pub, freePort(t)
	lab.user = User{ID: "6a1f3a2e-1111-4c3b-9a55-0123456789ab", Email: "cfg-1@proxyhub"}
	lab.x = &desired.Xray{
		Port:        freePort(t),
		PrivateKey:  priv,
		Target:      lab.target.Listener.Addr().String(),
		ServerNames: []string{"example.com"},
		ShortIDs:    []string{"0123456789abcdef"},
	}
	return lab
}

func (lab *realityLab) render(t *testing.T) []byte {
	t.Helper()
	cfg, err := Render(lab.x, []User{lab.user}, fmt.Sprintf("127.0.0.1:%d", lab.apiPort), TLSFiles{})
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

// start runs the server with serverCfg and a client whose two local ports lead, through REALITY,
// to the private service on loopback and on the LAN address.
func (lab *realityLab) start(t *testing.T, serverCfg []byte) (api *API, loopPort, lanPort int, serverLog *lockedBuffer) {
	t.Helper()
	serverLog = startXray(t, lab.bin, lab.assets, serverCfg)
	api = NewAPI(fmt.Sprintf("127.0.0.1:%d", lab.apiPort))
	waitFor(t, "the server's API", func() bool { _, err := api.Uptime(context.Background()); return err == nil })
	loopPort, lanPort = freePort(t), freePort(t)
	forward := func(port int, address string) map[string]any {
		return map[string]any{
			"listen": "127.0.0.1", "port": port, "protocol": "dokodemo-door",
			"settings": map[string]any{"address": address, "port": lab.privatePort, "network": "tcp"},
		}
	}
	clientCfg := map[string]any{
		"log":      map[string]any{"loglevel": "warning", "access": "none"},
		"inbounds": []any{forward(loopPort, "127.0.0.1"), forward(lanPort, lab.lan)},
		"outbounds": []any{map[string]any{
			"protocol": "vless",
			"settings": map[string]any{"vnext": []any{map[string]any{
				"address": "127.0.0.1", "port": lab.x.Port,
				"users": []any{map[string]any{"id": lab.user.ID, "encryption": "none", "flow": Flow}},
			}}},
			"streamSettings": map[string]any{
				"network": "tcp", "security": "reality",
				"realitySettings": map[string]any{
					"serverName": "example.com", "fingerprint": "chrome", "password": lab.pub, "shortId": "0123456789abcdef",
				},
			},
		}},
	}
	clientJSON, _ := json.Marshal(clientCfg)
	startXray(t, lab.bin, lab.assets, clientJSON)
	waitFor(t, "the client's listener", func() bool {
		c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", lanPort), time.Second)
		if err == nil {
			_ = c.Close()
		}
		return err == nil
	})
	return api, loopPort, lanPort, serverLog
}

// request sends one HTTP request into a client port and returns the first response line ("" if
// the connection ended without an answer).
func request(t *testing.T, port int) string {
	t.Helper()
	c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(4 * time.Second))
	_, _ = io.WriteString(c, "GET / HTTP/1.1\r\nHost: private.test\r\nConnection: close\r\n\r\n")
	line, _ := bufio.NewReader(c).ReadString('\n')
	return line
}

func userTraffic(t *testing.T, api *API, email string) uint64 {
	t.Helper()
	m, err := api.UserTraffic(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return m[email]
}

// Positive control for the test below: with the private-range blocks taken out of the rendered
// config and only the LAN address explicitly allowed, a request really travels through REALITY and
// XTLS Vision, the service answers, and the user's counter covers request and response. So the
// path works, and in the real config it is the blocks that stop it.
func TestRealXrayCarriesTrafficWhenAllowed(t *testing.T) {
	lab := newRealityLab(t)
	var m map[string]any
	if err := json.Unmarshal(lab.render(t), &m); err != nil {
		t.Fatal(err)
	}
	m["routing"].(map[string]any)["rules"] = []any{}
	m["outbounds"].([]any)[0].(map[string]any)["settings"] = map[string]any{
		"finalRules": []any{map[string]any{"action": "allow", "ip": []any{lab.lan + "/32"}}},
	}
	open, _ := json.Marshal(m)
	api, _, lanPort, _ := lab.start(t, open)
	if got := request(t, lanPort); !strings.HasPrefix(got, "HTTP/1.1 200") {
		t.Fatalf("no answer through the proxy: %q", got)
	}
	if lab.hits.Load() != 1 {
		t.Fatalf("hits %d", lab.hits.Load())
	}
	var seen uint64
	waitFor(t, "request and response bytes", func() bool {
		seen = userTraffic(t, api, lab.user.Email)
		return seen > 150
	})
	// Reading the counters never resets them (the agent's tracker relies on cumulative totals).
	if again := userTraffic(t, api, lab.user.Email); again < seen {
		t.Fatalf("reading the counters reset them: %d -> %d", seen, again)
	}
}

// The whole Reality path with the rendered config, the pinned binary on both ends: a user connects
// through a real REALITY handshake, is counted by the API the agent uses, is refused after a live
// RemoveUser and admitted again after a live AddUser. The destinations are the service on loopback
// (like the node's own API ports) and on the private LAN address: both are blocked, so the service
// never sees a request. The server's own log output never shows an address (rule 7).
func TestRealXrayRealityUsersCountersAndBlocking(t *testing.T) {
	lab := newRealityLab(t)
	api, loopPort, lanPort, serverLog := lab.start(t, lab.render(t))
	ctx := context.Background()
	email := lab.user.Email

	if got := request(t, loopPort); got != "" {
		t.Fatalf("a loopback destination answered through the proxy: %q", got)
	}
	if got := request(t, lanPort); got != "" {
		t.Fatalf("a private destination answered through the proxy: %q", got)
	}
	waitFor(t, "the user's traffic counter", func() bool { return userTraffic(t, api, email) > 0 })
	counted := userTraffic(t, api, email)

	// Live removal: the same client is no longer admitted and its counter stays put.
	if err := api.RemoveUser(ctx, InboundTag, email); err != nil {
		t.Fatalf("RemoveUser: %v", err)
	}
	_ = request(t, lanPort)
	time.Sleep(500 * time.Millisecond)
	if after := userTraffic(t, api, email); after != counted {
		t.Fatalf("a removed user was still served: %d -> %d bytes", counted, after)
	}
	// Removing twice is an error the agent sees (and answers with a restart).
	var status *StatusError
	if err := api.RemoveUser(ctx, InboundTag, email); !errors.As(err, &status) {
		t.Fatalf("second RemoveUser: %v", err)
	}
	// Live add: admitted again.
	if err := api.AddUser(ctx, InboundTag, Flow, lab.user); err != nil {
		t.Fatalf("AddUser: %v", err)
	}
	if err := api.AddUser(ctx, InboundTag, Flow, lab.user); !errors.As(err, &status) {
		t.Fatalf("duplicate AddUser: %v", err)
	}
	_ = request(t, lanPort)
	waitFor(t, "traffic after the user came back", func() bool { return userTraffic(t, api, email) > counted })

	if lab.hits.Load() != 0 {
		t.Fatalf("the private service was reached %d times through the proxy", lab.hits.Load())
	}
	if out := serverLog.String(); ipv4Like.MatchString(out) {
		t.Fatalf("the server's output shows an address:\n%s", out)
	}
}

// --- VLESS + WebSocket + TLS (transport ws_tls) ---------------------------------------------------

// wsLab is the ws_tls setup on loopback: the rendered server with a throwaway CA's certificate for
// the node's name in files (as the agent writes them), a real Xray client that verifies that
// certificate with the CA (fingerprint chrome, ALPN http/1.1, the link's exact shape), and the
// counting private service of the REALITY lab.
type wsLab struct {
	bin, assets string
	m           *testcert.Material
	caFile      string
	hits        atomic.Int64
	lan         string
	privatePort int
	user        User
	x           *desired.Xray
	files       TLSFiles
	apiPort     int
}

func newWSLab(t *testing.T) *wsLab {
	t.Helper()
	bin, assets := pinnedXray(t)
	lab := &wsLab{bin: bin, assets: assets, lan: localPrivateIPv4()}
	if lab.lan == "" {
		t.Skip("this machine has no private IPv4 address to test the private-range block with")
	}
	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	private := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		lab.hits.Add(1)
		_, _ = io.WriteString(w, "private")
	}))
	private.Listener = listener
	private.Start()
	t.Cleanup(private.Close)
	lab.privatePort = listener.Addr().(*net.TCPAddr).Port

	lab.m, err = testcert.New("*.ipoxxo-test.example")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	lab.caFile = filepath.Join(dir, "ca.pem")
	lab.files = TLSFiles{Cert: filepath.Join(dir, "node.crt"), Key: filepath.Join(dir, "node.key")}
	for path, content := range map[string][]byte{
		lab.caFile: lab.m.CAPEM, lab.files.Cert: []byte(lab.m.ChainPEM), lab.files.Key: []byte(lab.m.KeyPEM),
	} {
		if err := os.WriteFile(path, content, 0o640); err != nil {
			t.Fatal(err)
		}
	}
	lab.apiPort = freePort(t)
	lab.user = User{ID: "6a1f3a2e-1111-4c3b-9a55-0123456789ab", Email: "cfg-1@proxyhub"}
	lab.x = &desired.Xray{
		Transport:    desired.TransportWSTLS,
		Port:         freePort(t),
		ServerName:   wsTestName,
		WSPath:       "/0123456789abcdef",
		TLSCertChain: lab.m.ChainPEM,
		TLSKey:       lab.m.KeyPEM,
	}
	if err := lab.x.Validate(nil); err != nil {
		t.Fatal(err)
	}
	return lab
}

func (lab *wsLab) render(t *testing.T) []byte {
	t.Helper()
	cfg, err := Render(lab.x, []User{lab.user}, fmt.Sprintf("127.0.0.1:%d", lab.apiPort), lab.files)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

// client starts a real Xray client whose two local ports lead, through VLESS + WS + TLS on path, to
// the private service on loopback and on the LAN address. trustCA false: the client knows only the
// system roots (so it must refuse the test certificate).
func (lab *wsLab) client(t *testing.T, path string, trustCA bool) (loopPort, lanPort int) {
	t.Helper()
	loopPort, lanPort = freePort(t), freePort(t)
	forward := func(port int, address string) map[string]any {
		return map[string]any{
			"listen": "127.0.0.1", "port": port, "protocol": "dokodemo-door",
			"settings": map[string]any{"address": address, "port": lab.privatePort, "network": "tcp"},
		}
	}
	clientCfg := map[string]any{
		"log":      map[string]any{"loglevel": "warning", "access": "none"},
		"inbounds": []any{forward(loopPort, "127.0.0.1"), forward(lanPort, lab.lan)},
		"outbounds": []any{map[string]any{
			"protocol": "vless",
			"settings": map[string]any{"vnext": []any{map[string]any{
				"address": "127.0.0.1", "port": lab.x.Port,
				"users": []any{map[string]any{"id": lab.user.ID, "encryption": "none"}},
			}}},
			// The published link: security=tls&sni=<name>&alpn=http/1.1&fp=chrome&type=ws&host=<name>&path=...
			"streamSettings": map[string]any{
				"network": "ws", "security": "tls",
				"tlsSettings": map[string]any{
					"serverName": wsTestName, "alpn": []any{ALPN}, "fingerprint": "chrome",
					// Trust only the test CA (no allowInsecure): the certificate is really verified.
					"certificates": []any{map[string]any{"usage": "verify", "certificateFile": lab.caFile}},
				},
				"wsSettings": map[string]any{"path": path, "host": wsTestName},
			},
		}},
	}
	if !trustCA {
		tlsCfg := clientCfg["outbounds"].([]any)[0].(map[string]any)["streamSettings"].(map[string]any)["tlsSettings"]
		delete(tlsCfg.(map[string]any), "certificates")
	}
	clientJSON, _ := json.Marshal(clientCfg)
	startXray(t, lab.bin, lab.assets, clientJSON)
	waitFor(t, "the client's listener", func() bool {
		c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", lanPort), time.Second)
		if err == nil {
			_ = c.Close()
		}
		return err == nil
	})
	return loopPort, lanPort
}

func (lab *wsLab) server(t *testing.T, cfg []byte) (*API, *lockedBuffer) {
	t.Helper()
	out := startXray(t, lab.bin, lab.assets, cfg)
	api := NewAPI(fmt.Sprintf("127.0.0.1:%d", lab.apiPort))
	waitFor(t, "the server's API", func() bool { _, err := api.Uptime(context.Background()); return err == nil })
	return api, out
}

// Positive control and the node's own check: with the private-range blocks taken out and only the
// LAN address allowed, a request travels through a real VLESS + WebSocket + TLS connection (the
// client verifies the certificate for the node's name with the CA) and is counted for the user. A
// client on another WebSocket path is not served, nor one that does not trust the certificate (the
// verification is real, not "allowInsecure"). The agent's own TLS check of the inbound passes
// with the CA as root, and fails without it (system roots) or for another name.
func TestRealXrayWSTLSCarriesTrafficWhenAllowed(t *testing.T) {
	lab := newWSLab(t)
	cfgPath := filepath.Join(t.TempDir(), "config.json")
	if err := WriteConfig(cfgPath, lab.render(t), fsutil.KeepOwner); err != nil {
		t.Fatal(err)
	}
	if err := (Service{Binary: lab.bin, AssetsDir: lab.assets}).Test(context.Background(), cfgPath); err != nil {
		t.Fatalf("the pinned Xray refuses the rendered ws_tls config: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(lab.render(t), &m); err != nil {
		t.Fatal(err)
	}
	m["routing"].(map[string]any)["rules"] = []any{}
	m["outbounds"].([]any)[0].(map[string]any)["settings"] = map[string]any{
		"finalRules": []any{map[string]any{"action": "allow", "ip": []any{lab.lan + "/32"}}},
	}
	open, _ := json.Marshal(m)
	api, _ := lab.server(t, open)
	_, lanPort := lab.client(t, lab.x.WSPath, true)
	if got := request(t, lanPort); !strings.HasPrefix(got, "HTTP/1.1 200") {
		t.Fatalf("no answer through VLESS + WS + TLS: %q", got)
	}
	if lab.hits.Load() != 1 {
		t.Fatalf("hits %d", lab.hits.Load())
	}
	waitFor(t, "request and response bytes", func() bool { return userTraffic(t, api, lab.user.Email) > 150 })

	_, wrongPort := lab.client(t, "/not-the-path", true)
	if got := request(t, wrongPort); got != "" {
		t.Fatalf("a client on another path was served: %q", got)
	}
	_, untrusting := lab.client(t, lab.x.WSPath, false)
	if got := request(t, untrusting); got != "" {
		t.Fatalf("a client without the CA was served: %q", got)
	}
	if lab.hits.Load() != 1 {
		t.Fatalf("the wrong path or an untrusted handshake reached the service: %d hits", lab.hits.Load())
	}

	ctx := context.Background()
	if err := CheckOwnTLS(ctx, lab.x.Port, wsTestName, lab.m.Pool); err != nil {
		t.Fatalf("own TLS check with the right root: %v", err)
	}
	if err := CheckOwnTLS(ctx, lab.x.Port, wsTestName, nil); err == nil {
		t.Fatal("own TLS check passed a certificate the system roots do not know")
	}
	if err := CheckOwnTLS(ctx, lab.x.Port, "slot3.elsewhere.example", lab.m.Pool); err == nil {
		t.Fatal("own TLS check passed for a name the certificate does not cover")
	}
}

// The rendered ws_tls config as it is, with the pinned binary on both ends: the user is counted,
// private destinations (loopback, LAN) are blocked, a live RemoveUser in the WebSocket inbound
// stops the user and a live AddUser (no flow) brings it back. Xray's own output shows no address.
func TestRealXrayWSTLSUsersCountersAndBlocking(t *testing.T) {
	lab := newWSLab(t)
	api, serverLog := lab.server(t, lab.render(t))
	loopPort, lanPort := lab.client(t, lab.x.WSPath, true)
	ctx := context.Background()
	email := lab.user.Email

	if got := request(t, loopPort); got != "" {
		t.Fatalf("a loopback destination answered through the proxy: %q", got)
	}
	if got := request(t, lanPort); got != "" {
		t.Fatalf("a private destination answered through the proxy: %q", got)
	}
	waitFor(t, "the user's traffic counter", func() bool { return userTraffic(t, api, email) > 0 })
	counted := userTraffic(t, api, email)

	if err := api.RemoveUser(ctx, WSInboundTag, email); err != nil {
		t.Fatalf("RemoveUser: %v", err)
	}
	_ = request(t, lanPort)
	time.Sleep(500 * time.Millisecond)
	if after := userTraffic(t, api, email); after != counted {
		t.Fatalf("a removed user was still served: %d -> %d bytes", counted, after)
	}
	if err := api.AddUser(ctx, WSInboundTag, "", lab.user); err != nil {
		t.Fatalf("AddUser: %v", err)
	}
	_ = request(t, lanPort)
	waitFor(t, "traffic after the user came back", func() bool { return userTraffic(t, api, email) > counted })

	if lab.hits.Load() != 0 {
		t.Fatalf("the private service was reached %d times through the proxy", lab.hits.Load())
	}
	if out := serverLog.String(); ipv4Like.MatchString(out) {
		t.Fatalf("the server's output shows an address:\n%s", out)
	}
}
