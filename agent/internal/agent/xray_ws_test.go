package agent

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/testcert"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/xray"
)

const wsName = "t0a1b2c3d.ipoxxo-test.example"

func wsDoc(t *testing.T, m *testcert.Material, users string) *desired.Document {
	t.Helper()
	section := map[string]any{
		"transport": "ws_tls", "port": 443, "server_name": wsName, "ws_path": "/0123456789abcdef",
		"tls_cert_chain": m.ChainPEM, "tls_key": m.KeyPEM, "users": json.RawMessage("[" + users + "]"),
	}
	raw, err := json.Marshal(section)
	if err != nil {
		t.Fatal(err)
	}
	body := `{"format":1,"node_id":"` + node + `","intervals":{"poll_s":15,"report_s":60},"telemt":null,"xray":` + string(raw) + `}`
	doc, err := desired.Decode([]byte(body), node)
	if err != nil {
		t.Fatal(err)
	}
	return doc
}

// The agent and a ws_tls section: the certificate and key are written as files (0640) in the
// certificate directory and only their names go into Xray's config; the node checks its own inbound
// once Xray runs the config (not before), reports that check for "<name>:443", repeats a failed one
// after a minute and a passed one after six hours, and checks again at once after a renewal. A
// renewed certificate gets new files and a restart; the old files go once nothing names them, and
// all of them go when the node switches back to REALITY. No key material reaches a log line.
func TestXrayWSTLSFilesAndOwnCheck(t *testing.T) {
	dir := t.TempDir()
	caPath := writeIdentity(t, dir)
	cfg := &config.Config{
		PanelURL: "http://" + closedPort(t), NodeID: node, CAFile: caPath,
		TokenFile: filepath.Join(dir, "token"), StateDir: filepath.Join(dir, "state"),
		TelemtConfig: filepath.Join(dir, "telemt.toml"), TelemtDataDir: "/var/lib/telemt",
		MetricsListen: closedPort(t),
	}
	logs := &lockedBuffer{}
	a, err := New(cfg, "test", slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 11, 5, 12, 0, 0, 0, time.UTC)
	a.now = func() time.Time { return now }
	bin, assets := pinnedXrayOrEmpty(t)
	withBinary := bin != ""
	if !withBinary {
		bin, assets = "/nonexistent/xray", dir
	}
	restarts := filepath.Join(dir, "restarts")
	xcfg := filepath.Join(dir, "xray", "config.json")
	tlsDir := filepath.Join(dir, "xray", "tls")
	if err := os.MkdirAll(filepath.Dir(xcfg), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := a.SetXray(&xray.Settings{
		Config: xcfg, Binary: bin, AssetsDir: assets, API: closedPort(t), Version: "26.10.31",
		RestartCommand: []string{"/bin/sh", "-c", "echo r >> " + restarts},
		StopCommand:    []string{"/bin/true"},
	}); err != nil {
		t.Fatal(err)
	}
	if !withBinary {
		a.xr.service.Binary = ""
	}
	if a.xr.settings.CertDir() != tlsDir {
		t.Fatalf("certificate dir %s", a.xr.settings.CertDir())
	}
	var ownChecks []string
	ownErr := error(nil)
	a.xr.checkOwnTLS = func(_ context.Context, port int, name string, _ *x509.CertPool) error {
		ownChecks = append(ownChecks, name)
		if port != 443 {
			t.Errorf("own check on port %d", port)
		}
		return ownErr
	}
	realityChecks := 0
	a.xr.checkTarget = func(context.Context, string, string, *x509.CertPool) error { realityChecks++; return nil }
	count := func() int { data, _ := os.ReadFile(restarts); return strings.Count(string(data), "\n") }
	certFiles := func() []string {
		entries, _ := os.ReadDir(tlsDir)
		var names []string
		for _, e := range entries {
			names = append(names, e.Name())
		}
		return names
	}
	reported := func() (any, any, any) {
		x := a.xrayReport()
		if x.TargetOK == nil {
			return nil, nil, nil
		}
		return *x.TargetOK, *x.CheckedTarget, *x.CheckedServerName
	}

	first, err := testcert.New("*.ipoxxo-test.example")
	if err != nil {
		t.Fatal(err)
	}
	a.doc = wsDoc(t, first, userC)

	// 1. Files written, config names them, Xray restarted; no own check yet (Xray just restarted).
	if err := a.ApplyXray(t.Context()); err != nil {
		t.Fatal(err)
	}
	files := certFiles()
	if len(files) != 2 || count() != 1 || len(ownChecks) != 0 || realityChecks != 0 {
		t.Fatalf("files %v restarts %d checks %v %d", files, count(), ownChecks, realityChecks)
	}
	if st, _ := os.Stat(tlsDir); st.Mode().Perm() != 0o750 {
		t.Fatalf("certificate dir mode %v", st.Mode().Perm())
	}
	data, _ := os.ReadFile(xcfg)
	for _, f := range files {
		st, err := os.Stat(filepath.Join(tlsDir, f))
		if err != nil || st.Mode().Perm() != 0o640 {
			t.Fatalf("%s: %v %v", f, err, st.Mode().Perm())
		}
		if !strings.Contains(string(data), filepath.Join(tlsDir, f)) {
			t.Fatalf("the config does not name %s", f)
		}
	}
	key, _ := os.ReadFile(filepath.Join(tlsDir, strings.TrimSuffix(files[0], ".crt")+".key"))
	if string(key) != first.KeyPEM || strings.Contains(string(data), "PRIVATE KEY") {
		t.Fatal("key file content, or key material inside the config")
	}
	if ok, _, _ := reported(); ok != nil {
		t.Fatal("a check was reported before any was made")
	}

	// 2. Xray answers and runs this config: the own check runs and is reported.
	a.xr.up = true
	if err := a.ApplyXray(t.Context()); err != nil {
		t.Fatal(err)
	}
	if len(ownChecks) != 1 || ownChecks[0] != wsName || count() != 1 {
		t.Fatalf("checks %v restarts %d", ownChecks, count())
	}
	if ok, target, name := reported(); ok != true || target != wsName+":443" || name != wsName {
		t.Fatalf("reported %v %v %v", ok, target, name)
	}
	now = now.Add(time.Hour)
	_ = a.ApplyXray(t.Context())
	if len(ownChecks) != 1 {
		t.Fatal("a passed check repeated within six hours")
	}

	// 3. Six hours on it fails; a failed check is repeated after a minute, not sooner.
	ownErr = errors.New("own tls: no handshake")
	now = now.Add(targetRecheck)
	_ = a.ApplyXray(t.Context())
	if ok, _, _ := reported(); len(ownChecks) != 2 || ok != false {
		t.Fatalf("checks %v reported %v", ownChecks, ok)
	}
	now = now.Add(30 * time.Second)
	_ = a.ApplyXray(t.Context())
	now = now.Add(ownTLSRetry)
	ownErr = nil
	_ = a.ApplyXray(t.Context())
	if ok, _, _ := reported(); len(ownChecks) != 3 || ok != true {
		t.Fatalf("checks %v reported %v", ownChecks, ok)
	}

	// 4. Renewal: new files, a restart onto them, the old files removed, and checked again at once.
	renewed, err := testcert.New("*.ipoxxo-test.example")
	if err != nil {
		t.Fatal(err)
	}
	a.doc = wsDoc(t, renewed, userC)
	a.xr.up = false
	if err := a.ApplyXray(t.Context()); err != nil {
		t.Fatal(err)
	}
	after := certFiles()
	if count() != 2 || len(after) != 2 || after[0] == files[0] {
		t.Fatalf("renewal: restarts %d files %v (before %v)", count(), after, files)
	}
	a.xr.up = true
	_ = a.ApplyXray(t.Context())
	if len(ownChecks) != 4 {
		t.Fatalf("a renewed certificate was not checked: %v", ownChecks)
	}

	// 5. Back to REALITY: the target is checked, the report follows it, the certificate files go.
	cached := xrayDoc(userC)
	doc, err := desired.Decode([]byte(cached), node)
	if err != nil {
		t.Fatal(err)
	}
	a.doc = doc
	if ok, _, _ := reported(); ok != nil {
		t.Fatal("the ws_tls check was reported for a REALITY section")
	}
	if err := a.ApplyXray(t.Context()); err != nil {
		t.Fatal(err)
	}
	if realityChecks != 1 || count() != 3 || len(certFiles()) != 0 {
		t.Fatalf("reality: checks %d restarts %d files %v", realityChecks, count(), certFiles())
	}
	if ok, target, _ := reported(); ok != true || target != "dl.google.com:443" {
		t.Fatalf("reported %v %v", ok, target)
	}

	out := logs.String()
	for _, m := range []*testcert.Material{first, renewed} {
		if strings.Contains(out, strings.Split(m.KeyPEM, "\n")[1][:24]) || strings.Contains(out, "PRIVATE KEY") {
			t.Fatal("a log line carries key material")
		}
	}
}
