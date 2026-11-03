package agent

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
)

// testPanel is a real HTTP server playing the panel's /agent/v1 routes in this unit test:
// it serves a chosen desired document and records every report the agent posts.
type testPanel struct {
	mu      sync.Mutex
	body    string // desired document served with status 200, or "" for 304
	etag    string
	reports []map[string]any
	ifNone  []string
}

func (p *testPanel) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	p.mu.Lock()
	defer p.mu.Unlock()
	switch r.URL.Path {
	case "/agent/v1/desired":
		p.ifNone = append(p.ifNone, r.Header.Get("If-None-Match"))
		if p.body == "" {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		w.Header().Set("ETag", p.etag)
		_, _ = io.WriteString(w, p.body)
	case "/agent/v1/report":
		var m map[string]any
		if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		p.reports = append(p.reports, m)
		w.WriteHeader(http.StatusNoContent)
	default:
		http.NotFound(w, r)
	}
}

func (p *testPanel) last(t *testing.T) map[string]any {
	t.Helper()
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.reports) == 0 {
		t.Fatal("no report was posted")
	}
	return p.reports[len(p.reports)-1]
}

// A desired document the agent refuses must reach the panel as apply_error, even though the cached
// state keeps being applied successfully meanwhile (it used to be cleared by that apply), and the
// error must clear once the panel serves an acceptable state again.
func TestRefusedDesiredStateIsReported(t *testing.T) {
	dir := t.TempDir()
	caPath := writeIdentity(t, dir)
	panelSrv := &testPanel{
		body: strings.Replace(cachedDoc, `"beobachten":false`, `"beobachten":true`, 1),
		etag: `"` + strings.Repeat("b", 64) + `"`,
	}
	srv := httptest.NewServer(panelSrv)
	defer srv.Close()

	cfg := &config.Config{
		PanelURL: srv.URL, NodeID: node, CAFile: caPath,
		TokenFile: filepath.Join(dir, "token"), StateDir: filepath.Join(dir, "state"),
		TelemtConfig: filepath.Join(dir, "telemt.toml"), TelemtDataDir: "/var/lib/telemt",
		MetricsListen: closedPort(t),
	}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	cachedETag := `"` + strings.Repeat("a", 64) + `"`
	cached, _ := json.Marshal(cachedDesired{ETag: cachedETag, Body: json.RawMessage(cachedDoc)})
	if err := os.WriteFile(cfg.DesiredFile(), cached, 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := New(cfg, "test", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	a.now = func() time.Time { return time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC) }

	for round := range 2 { // the refusal persists across polls while the panel keeps serving it
		if err := a.Once(t.Context()); err == nil {
			t.Fatalf("round %d: expected the refused desired state as an error", round)
		}
		r := panelSrv.last(t)
		if r["apply_ok"] != false || r["apply_error"] != "desired_invalid" || r["applied_etag"] != cachedETag {
			t.Fatalf("round %d: report = ok %v error %v applied %v", round, r["apply_ok"], r["apply_error"], r["applied_etag"])
		}
		if _, err := os.Stat(cfg.TelemtConfig); err != nil {
			t.Fatalf("round %d: the cached state was not applied: %v", round, err)
		}
	}
	data, _ := os.ReadFile(cfg.TelemtConfig)
	if strings.Contains(string(data), "beobachten = true") {
		t.Fatal("the refused document reached the Telemt config")
	}

	// The panel goes back to the state the node holds: 304, and the error clears.
	panelSrv.mu.Lock()
	panelSrv.body = ""
	panelSrv.mu.Unlock()
	if err := a.Once(t.Context()); err != nil {
		t.Fatalf("unchanged state: %v", err)
	}
	r := panelSrv.last(t)
	if r["apply_ok"] != true || r["apply_error"] != nil || r["applied_etag"] != cachedETag {
		t.Fatalf("after recovery: report = ok %v error %v applied %v", r["apply_ok"], r["apply_error"], r["applied_etag"])
	}
	panelSrv.mu.Lock()
	defer panelSrv.mu.Unlock()
	for _, h := range panelSrv.ifNone {
		if h != cachedETag {
			t.Fatalf("If-None-Match %q: the refused document's ETag must never be adopted", h)
		}
	}
}
