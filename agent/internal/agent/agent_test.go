package agent

import (
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/pki"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/telemt"
)

const node = "0b7f6a3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"

// closedPort returns a loopback port nothing listens on: the panel is unreachable.
func closedPort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr
}

// writeIdentity provisions a CA and a client certificate the way registration would leave them.
func writeIdentity(t *testing.T, dir string) string {
	t.Helper()
	caKey, _ := pki.GenerateKey()
	caTmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test CA"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(365 * 24 * time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	caDER, _ := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	caCert, _ := x509.ParseCertificate(caDER)
	caPath := filepath.Join(dir, "ca.pem")
	if err := os.WriteFile(caPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}), 0o644); err != nil {
		t.Fatal(err)
	}
	key, _ := pki.GenerateKey()
	uri, _ := url.Parse(pki.NodeURIPrefix + node)
	leaf := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: node},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(30 * 24 * time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		URIs: []*url.URL{uri},
	}
	der, _ := x509.CreateCertificate(rand.Reader, leaf, caCert, &key.PublicKey, caKey)
	state := filepath.Join(dir, "state")
	if err := os.MkdirAll(state, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := pki.SaveIdentity(filepath.Join(state, "identity.pem"), key, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})); err != nil {
		t.Fatal(err)
	}
	return caPath
}

const cachedDoc = `{"format":1,"node_id":"` + node + `","intervals":{"poll_s":15,"report_s":60},"telemt":{
"ports":[443],"tls_domain":"cdn.example.com","modes":{"classic":false,"secure":false,"tls":true},
"ad_tag":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","use_middle_proxy":true,"beobachten":false,"log_level":"silent",
"users":[
 {"name":"alice","secret":"0123456789abcdef0123456789abcdef","enabled":true,"ad_tag":null,
  "expires_at":"2026-11-01T00:00:00Z","data_quota_bytes":null,"max_tcp_conns":null},
 {"name":"bob","secret":"11111111111111111111111111111111","enabled":true,"ad_tag":null,
  "expires_at":"2026-12-01T00:00:00Z","data_quota_bytes":null,"max_tcp_conns":null},
 {"name":"sponsor_b","secret":"fedcba9876543210fedcba9876543210","enabled":true,
  "ad_tag":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","expires_at":null,"data_quota_bytes":100000,"max_tcp_conns":null}
]}}`

// With the panel unreachable, the agent applies its cached desired state and enforces expiry and
// quota itself (docs/09 R10), using real Telemt metrics served over a real HTTP listener.
func TestOfflineEnforcementFromCachedState(t *testing.T) {
	dir := t.TempDir()
	caPath := writeIdentity(t, dir)
	fixture, err := os.ReadFile("../metrics/testdata/telemt-3.5.16-metrics.txt")
	if err != nil {
		t.Fatal(err)
	}
	metricsSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/metrics" {
			t.Errorf("agent requested %s from Telemt", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(fixture)
	}))
	defer metricsSrv.Close()

	cfg := &config.Config{
		PanelURL:      "http://" + closedPort(t),
		NodeID:        node,
		CAFile:        caPath,
		TokenFile:     filepath.Join(dir, "token"),
		StateDir:      filepath.Join(dir, "state"),
		TelemtConfig:  filepath.Join(dir, "telemt.toml"),
		TelemtDataDir: "/var/lib/telemt",
		MetricsListen: strings.TrimPrefix(metricsSrv.URL, "http://"),
	}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	cached, _ := json.Marshal(cachedDesired{ETag: `"` + strings.Repeat("a", 64) + `"`, Body: json.RawMessage(cachedDoc)})
	if err := os.WriteFile(cfg.DesiredFile(), cached, 0o600); err != nil {
		t.Fatal(err)
	}

	a, err := New(cfg, "test", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	a.now = func() time.Time { return time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC) }
	if err := a.Once(t.Context()); err == nil {
		t.Fatal("expected the unreachable panel to be reported as an error")
	}

	data, err := os.ReadFile(cfg.TelemtConfig)
	if err != nil {
		t.Fatalf("cached state was not applied: %v", err)
	}
	if err := telemt.CheckInvariants(data); err != nil {
		t.Fatal(err)
	}
	doc, _ := telemt.ParseSubset(data)
	disabled := doc.Tables["access.user_enabled"].Values
	if v, ok := disabled["alice"]; !ok || v.Bool {
		t.Error("expired user alice still enabled")
	}
	if v, ok := disabled["sponsor_b"]; !ok || v.Bool {
		t.Error("over-quota user sponsor_b still enabled (430080 of 100000 bytes used)")
	}
	if _, ok := disabled["bob"]; ok {
		t.Error("bob is valid and must stay enabled")
	}
	st, _ := os.Stat(cfg.TelemtConfig)
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("telemt config mode %v", st.Mode().Perm())
	}
	st, _ = os.Stat(cfg.UsageFile())
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("usage file mode %v", st.Mode().Perm())
	}
	if a.appliedETag != `"`+strings.Repeat("a", 64)+`"` || a.applyErr != "" {
		t.Fatalf("applied %q err %q", a.appliedETag, a.applyErr)
	}
}

// A cached document that breaks an invariant is never applied, even offline.
func TestRefusedCachedStateIsNotApplied(t *testing.T) {
	dir := t.TempDir()
	caPath := writeIdentity(t, dir)
	cfg := &config.Config{
		PanelURL: "http://" + closedPort(t), NodeID: node, CAFile: caPath,
		TokenFile: filepath.Join(dir, "token"), StateDir: filepath.Join(dir, "state"),
		TelemtConfig: filepath.Join(dir, "telemt.toml"), TelemtDataDir: "/var/lib/telemt",
		MetricsListen: closedPort(t),
	}
	bad := strings.Replace(cachedDoc, `"beobachten":false`, `"beobachten":true`, 1)
	cached, _ := json.Marshal(cachedDesired{ETag: `"x"`, Body: json.RawMessage(bad)})
	if err := os.WriteFile(cfg.DesiredFile(), cached, 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := New(cfg, "test", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	_ = a.Once(t.Context())
	if _, err := os.Stat(cfg.TelemtConfig); !os.IsNotExist(err) {
		t.Fatal("a refused desired state produced a Telemt config")
	}
}
