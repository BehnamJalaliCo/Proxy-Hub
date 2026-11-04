package agent

import (
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/xray"
)

// xrayKey is a stable X25519 private key derived at run time (no key-shaped literal in the repo).
var xrayKey = func() string {
	sum := sha256.Sum256([]byte("proxyhub test key: agent"))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}()

func xrayDoc(users string) string {
	return `{"format":1,"node_id":"` + node + `","intervals":{"poll_s":15,"report_s":60},"telemt":null,
"xray":{"port":443,"private_key":"` + xrayKey + `","target":"dl.google.com:443",
"server_names":["dl.google.com"],"short_ids":["0123456789abcdef"],"users":[` + users + `]}}`
}

const (
	userA = `{"id":"6a1f3a2e-1111-4c3b-9a55-0123456789ab","email":"cfg-1@proxyhub","expires_at":"2026-11-05T00:00:00Z","data_cap_bytes":53687091200}`
	userB = `{"id":"11111111-2222-4333-8444-555555555555","email":"cfg-2@proxyhub","expires_at":"2026-11-03T00:00:00Z","data_cap_bytes":null}`
	userC = `{"id":"77777777-1111-4c3b-9a55-0123456789ab","email":"cfg-3@proxyhub","expires_at":null,"data_cap_bytes":null}`
)

// pinnedXrayOrEmpty returns the pinned binary when PROXYHUB_TEST_XRAY names it (then every staged
// config also passes "xray run -test"), else "" (the test step is skipped, nothing else changes).
func pinnedXrayOrEmpty(t *testing.T) (string, string) {
	t.Helper()
	bin := os.Getenv("PROXYHUB_TEST_XRAY")
	if bin == "" {
		return "", ""
	}
	data, err := os.ReadFile(bin)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	lock, _ := os.ReadFile("../../../node-image/versions.lock")
	if !strings.Contains(string(lock), "XCORE_BINARY_SHA256="+hex.EncodeToString(sum[:])) {
		t.Fatal("PROXYHUB_TEST_XRAY is not the pinned Xray build")
	}
	return bin, filepath.Dir(bin)
}

// The agent's Xray life cycle with the panel unreachable and the Xray API down (closed loopback
// port): first config with a restart, no-op on the same state, a user-only change that can not go
// live and therefore restarts, local expiry, and stop + removal once the section disappears. The
// report carries the Xray section without any address.
func TestXrayLifecycle(t *testing.T) {
	dir := t.TempDir()
	caPath := writeIdentity(t, dir)
	panelSrv := &testPanel{}
	srv := httptest.NewServer(panelSrv)
	defer srv.Close()
	cfg := &config.Config{
		PanelURL: srv.URL, NodeID: node, CAFile: caPath,
		TokenFile: filepath.Join(dir, "token"), StateDir: filepath.Join(dir, "state"),
		TelemtConfig: filepath.Join(dir, "telemt.toml"), TelemtDataDir: "/var/lib/telemt",
		MetricsListen: closedPort(t),
	}
	a, err := New(cfg, "test", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)
	a.now = func() time.Time { return now }

	bin, assets := pinnedXrayOrEmpty(t)
	testWithBinary := bin != ""
	if !testWithBinary {
		bin, assets = "/nonexistent/xray", dir
	}
	restarts, stops := filepath.Join(dir, "restarts"), filepath.Join(dir, "stops")
	xcfg := filepath.Join(dir, "xray", "config.json")
	if err := os.MkdirAll(filepath.Dir(xcfg), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := a.SetXray(&xray.Settings{
		Config: xcfg, Binary: bin, AssetsDir: assets, API: closedPort(t), Version: "26.10.31",
		RestartCommand: []string{"/bin/sh", "-c", "echo r >> " + restarts},
		StopCommand:    []string{"/bin/sh", "-c", "echo s >> " + stops},
	}); err != nil {
		t.Fatal(err)
	}
	if !testWithBinary {
		a.xr.service.Binary = "" // no config test without the pinned binary
	}
	checked := 0
	a.xr.checkTarget = func(_ context.Context, target, name string, _ *x509.CertPool) error {
		checked++
		if target != "dl.google.com:443" || name != "dl.google.com" {
			t.Errorf("target check for %s / %s", target, name)
		}
		return nil
	}
	count := func(path string) int {
		data, _ := os.ReadFile(path)
		return strings.Count(string(data), "\n")
	}
	serve := func(doc string) {
		panelSrv.mu.Lock()
		panelSrv.body, panelSrv.etag = doc, `"`+strings.Repeat("c", 63)+string(rune('a'+len(doc)%6))+`"`
		panelSrv.mu.Unlock()
	}

	// 1. First state: written (0600), tested, restarted once. userB has expired already.
	serve(xrayDoc(userA + "," + userB))
	if err := a.Once(t.Context()); err != nil {
		t.Fatalf("first cycle: %v", err)
	}
	data, err := os.ReadFile(xcfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := xray.CheckInvariants(data); err != nil {
		t.Fatal(err)
	}
	if st, _ := os.Stat(xcfg); st.Mode().Perm() != 0o600 {
		t.Fatalf("config mode %v", st.Mode().Perm())
	}
	if !strings.Contains(string(data), "cfg-1@proxyhub") || strings.Contains(string(data), "cfg-2@proxyhub") {
		t.Fatal("the expired user is served, or the valid one is missing")
	}
	if count(restarts) != 1 || checked != 1 {
		t.Fatalf("restarts %d, target checks %d", count(restarts), checked)
	}
	r := panelSrv.last(t)
	x, _ := r["xray"].(map[string]any)
	if x == nil || x["enabled"] != true || x["up"] != false || x["apply_error"] != nil || x["version"] != "26.10.31" || x["target_ok"] != true {
		t.Fatalf("report xray section %v", x)
	}
	body, _ := json.Marshal(r)
	if strings.Contains(string(body), xrayKey) || strings.Contains(string(body), "6a1f3a2e") {
		t.Fatal("the report carries the private key or a user id")
	}

	// 2. Same state: nothing to do. (The API is down, so after the grace period Xray is restarted:
	// keep inside it here.)
	a.xr.lastRestart = now
	if err := a.Once(t.Context()); err != nil {
		t.Fatal(err)
	}
	if count(restarts) != 1 {
		t.Fatalf("unchanged state restarted xray (%d)", count(restarts))
	}

	// 3. Users only: live would do, but the API is down, so it falls back to a tested restart.
	serve(xrayDoc(userA + "," + userC))
	if err := a.Once(t.Context()); err != nil {
		t.Fatal(err)
	}
	data, _ = os.ReadFile(xcfg)
	if count(restarts) != 2 || !strings.Contains(string(data), "cfg-3@proxyhub") {
		t.Fatalf("user change not applied (restarts %d)", count(restarts))
	}

	// 4. Time passes: userA expires on the node by itself (panel unreachable from now on).
	srv.Close()
	now = time.Date(2026, 11, 5, 0, 0, 1, 0, time.UTC)
	_ = a.Once(t.Context())
	data, _ = os.ReadFile(xcfg)
	if strings.Contains(string(data), "cfg-1@proxyhub") || !strings.Contains(string(data), "cfg-3@proxyhub") {
		t.Fatal("local expiry did not remove the user")
	}

	// 5. Xray down for longer than the grace period: restarted from the same file.
	before := count(restarts)
	now = now.Add(downRestart + time.Second)
	_ = a.Once(t.Context())
	if count(restarts) != before+1 {
		t.Fatal("an Xray that does not answer was not restarted")
	}

	// 6. The section disappears: Xray is stopped and its config removed.
	a.doc.Xray = nil
	if err := a.ApplyXray(t.Context()); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(xcfg); !errors.Is(err, os.ErrNotExist) || count(stops) != 1 {
		t.Fatalf("xray not stopped/removed (stops %d, stat %v)", count(stops), err)
	}
}

// A failing "xray run -test" (here: a binary that refuses everything) never replaces the config or
// restarts Xray, and the report says why.
func TestXrayRefusedConfigIsNotPromoted(t *testing.T) {
	dir := t.TempDir()
	caPath := writeIdentity(t, dir)
	cfg := &config.Config{
		PanelURL: "http://" + closedPort(t), NodeID: node, CAFile: caPath,
		TokenFile: filepath.Join(dir, "token"), StateDir: filepath.Join(dir, "state"),
		TelemtConfig: filepath.Join(dir, "telemt.toml"), TelemtDataDir: "/var/lib/telemt",
		MetricsListen: closedPort(t),
	}
	cached, _ := json.Marshal(cachedDesired{ETag: `"` + strings.Repeat("d", 64) + `"`, Body: json.RawMessage(xrayDoc(userC))})
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfg.DesiredFile(), cached, 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := New(cfg, "test", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	restarts := filepath.Join(dir, "restarts")
	xcfg := filepath.Join(dir, "config.json")
	if err := a.SetXray(&xray.Settings{
		Config: xcfg, Binary: "/bin/false", AssetsDir: dir, API: closedPort(t),
		RestartCommand: []string{"/bin/sh", "-c", "echo r >> " + restarts},
	}); err != nil {
		t.Fatal(err)
	}
	a.xr.checkTarget = func(context.Context, string, string, *x509.CertPool) error { return errors.New("no") }
	if err := a.ApplyXray(t.Context()); err == nil {
		t.Fatal("expected the refused config as an error")
	}
	if _, err := os.Stat(xcfg); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("a config the binary refused was promoted")
	}
	if _, err := os.Stat(restarts); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("xray was restarted on a refused config")
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".candidate-") {
			t.Fatal("the refused candidate was left behind")
		}
	}
	x := a.xrayReport()
	if x.ApplyError == nil || *x.ApplyError != "xray_config_refused" || x.TargetOK == nil || *x.TargetOK {
		t.Fatalf("report %+v", x)
	}
}
