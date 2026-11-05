package xray

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/enforce"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
)

// TestPanelDocument checks a desired-state document the real panel built (panel/tests/
// test_reality_db.py and test_wstls_db.py write it and run this test): the agent accepts it, renders
// it (REALITY, or VLESS + WS + TLS with the document's certificate written to files), and, with
// PROXYHUB_TEST_XRAY set, the pinned Xray binary accepts the rendered config. Nothing from the
// document (it holds a private key and user ids) is printed.
func TestPanelDocument(t *testing.T) {
	path := os.Getenv("PROXYHUB_TEST_DESIRED_DOC")
	if path == "" {
		t.Skip("run by the panel's tests with PROXYHUB_TEST_DESIRED_DOC")
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	doc, err := desired.Decode(body, os.Getenv("PROXYHUB_TEST_NODE_ID"))
	if err != nil {
		t.Fatalf("the agent refuses the panel's document: %v", err)
	}
	if doc.Xray == nil {
		t.Fatal("the document has no xray section")
	}
	users, _ := Effective(doc.Xray.Users, enforce.NewUsage(), time.Now())
	if want := os.Getenv("PROXYHUB_TEST_EXPECT_USERS"); want != "" && strconv.Itoa(len(users)) != want {
		t.Fatalf("%d users served, want %s", len(users), want)
	}
	if want := os.Getenv("PROXYHUB_TEST_EXPECT_TRANSPORT"); want != "" && (want == "ws_tls") != doc.Xray.WSTLS() {
		t.Fatalf("the document's transport is not %s", want)
	}
	dir := t.TempDir()
	var files TLSFiles
	if doc.Xray.WSTLS() {
		// The agent writes these from the document (0640); Xray's config only names them.
		files = TLSFiles{Cert: filepath.Join(dir, "panel.crt"), Key: filepath.Join(dir, "panel.key")}
		if err := os.WriteFile(files.Cert, []byte(doc.Xray.TLSCertChain), 0o640); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(files.Key, []byte(doc.Xray.TLSKey), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	data, err := Render(doc.Xray, users, "127.0.0.1:10085", files)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	out := filepath.Join(dir, "config.json")
	if err := WriteConfig(out, data, fsutil.KeepOwner); err != nil {
		t.Fatal(err)
	}
	if os.Getenv("PROXYHUB_TEST_XRAY") == "" {
		t.Log("panel document accepted and rendered (no Xray binary given)")
		return
	}
	bin, assets := pinnedXray(t)
	if err := (Service{Binary: bin, AssetsDir: assets}).Test(context.Background(), out); err != nil {
		t.Fatalf("the pinned Xray refuses the config rendered from the panel's document: %v", err)
	}
	t.Log("panel document accepted, rendered, and accepted by the pinned Xray")
}
