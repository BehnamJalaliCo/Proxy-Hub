package agent

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/xray"
)

// A REALITY section with an xhttp part: the agent writes the two-inbound config (the pinned binary
// accepts it when PROXYHUB_TEST_XRAY is set), and reports the XHTTP port only while Xray is up and
// the applied config has the inbound. When the panel drops the xhttp part, Xray restarts onto the
// one-inbound config and the port leaves the report.
func TestXrayXHTTPIsAppliedAndReported(t *testing.T) {
	dir := t.TempDir()
	caPath := writeIdentity(t, dir)
	cfg := &config.Config{
		PanelURL: "http://" + closedPort(t), NodeID: node, CAFile: caPath,
		TokenFile: filepath.Join(dir, "token"), StateDir: filepath.Join(dir, "state"),
		TelemtConfig: filepath.Join(dir, "telemt.toml"), TelemtDataDir: "/var/lib/telemt",
		MetricsListen: closedPort(t),
	}
	doc := strings.Replace(xrayDoc(userC), `"port":443,`, `"port":443,"xhttp":{"port":2083,"path":"/0123456789abcdef"},`, 1)
	cached, _ := json.Marshal(cachedDesired{ETag: `"` + strings.Repeat("f", 64) + `"`, Body: json.RawMessage(doc)})
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
	if a.doc == nil || a.doc.Xray == nil || a.doc.Xray.XHTTP == nil {
		t.Fatal("the cached document with an xhttp part was not loaded")
	}
	a.now = func() time.Time { return time.Date(2026, 11, 5, 12, 0, 0, 0, time.UTC) }
	bin, assets := pinnedXrayOrEmpty(t)
	testWithBinary := bin != ""
	if !testWithBinary {
		bin, assets = "/nonexistent/xray", dir
	}
	restarts := filepath.Join(dir, "restarts")
	xcfg := filepath.Join(dir, "config.json")
	if err := a.SetXray(&xray.Settings{
		Config: xcfg, Binary: bin, AssetsDir: assets, API: closedPort(t), Version: "26.10.31",
		RestartCommand: []string{"/bin/sh", "-c", "echo r >> " + restarts}, StopCommand: []string{"/bin/true"},
	}); err != nil {
		t.Fatal(err)
	}
	if !testWithBinary {
		a.xr.service.Binary = "" // no config test without the pinned binary
	}
	a.xr.checkTarget = func(context.Context, string, string, *x509.CertPool) error { return nil }

	if err := a.ApplyXray(t.Context()); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(xcfg)
	if err != nil {
		t.Fatal(err)
	}
	var c struct {
		Inbounds []struct {
			Tag  string `json:"tag"`
			Port int    `json:"port"`
		} `json:"inbounds"`
	}
	if err := json.Unmarshal(data, &c); err != nil || len(c.Inbounds) != 2 ||
		c.Inbounds[1].Tag != xray.XHTTPInboundTag || c.Inbounds[1].Port != 2083 {
		t.Fatalf("inbounds %+v %v", c.Inbounds, err)
	}
	if x := a.xrayReport(); x.XHTTPPort != nil {
		t.Fatal("xhttp port reported while Xray is down")
	}
	a.xr.up = true
	if x := a.xrayReport(); x.XHTTPPort == nil || *x.XHTTPPort != 2083 {
		t.Fatalf("xhttp port not reported: %v", x.XHTTPPort)
	}

	// The panel drops the xhttp part (the owner turned XHTTP off): restart onto one inbound.
	a.doc.Xray.XHTTP = (*desired.XHTTP)(nil)
	a.xr.up = false
	if err := a.ApplyXray(t.Context()); err != nil {
		t.Fatal(err)
	}
	data, _ = os.ReadFile(xcfg)
	if strings.Contains(string(data), xray.XHTTPInboundTag) {
		t.Fatal("the XHTTP inbound is still in the config")
	}
	got, _ := os.ReadFile(restarts)
	if strings.Count(string(got), "\n") != 2 {
		t.Fatalf("restarts %q", got)
	}
	a.xr.up = true
	if x := a.xrayReport(); x.XHTTPPort != nil {
		t.Fatal("xhttp port still reported without the inbound")
	}
}
