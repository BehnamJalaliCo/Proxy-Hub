package xray

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/enforce"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/testcert"
)

const wsTestName = "slot3.ipoxxo-test.example"

func wsDesired(t *testing.T, m *testcert.Material) *desired.Xray {
	t.Helper()
	x := testDesired()
	x.Transport = desired.TransportWSTLS
	x.PrivateKey, x.Target, x.ServerNames, x.ShortIDs = "", "", nil, nil
	x.ServerName, x.WSPath = wsTestName, "/0123456789abcdef"
	x.TLSCertChain, x.TLSKey = m.ChainPEM, m.KeyPEM
	if err := x.Validate(nil); err != nil {
		t.Fatal(err)
	}
	return x
}

func wsFiles(dir string) TLSFiles {
	return TLSFiles{Cert: filepath.Join(dir, "a.crt"), Key: filepath.Join(dir, "a.key")}
}

// The ws_tls inbound: VLESS (no decryption) over WebSocket on the scope's path, TLS with the
// certificate FILES only (never inline key material), ALPN http/1.1, TLS 1.2 at least; clients
// without a flow; the privacy settings exactly as for REALITY; no REALITY settings at all.
func TestRenderWSTLS(t *testing.T) {
	m, err := testcert.New("*.ipoxxo-test.example")
	if err != nil {
		t.Fatal(err)
	}
	x := wsDesired(t, m)
	users, _ := Effective(x.Users, enforce.NewUsage(), time.Now())
	files := wsFiles("/etc/xray/tls")
	data, err := Render(x, users, "127.0.0.1:10085", files)
	if err != nil {
		t.Fatal(err)
	}
	if err := CheckInvariants(data); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "PRIVATE KEY") || strings.Contains(string(data), "CERTIFICATE") {
		t.Fatal("the config carries certificate material instead of file names")
	}
	m2 := decode(t, data)
	log := m2["log"].(map[string]any)
	if log["access"] != "none" || log["maskAddress"] != "full" || log["dnsLog"] != false {
		t.Fatalf("log %v", log)
	}
	level := m2["policy"].(map[string]any)["levels"].(map[string]any)["0"].(map[string]any)
	if level["statsUserOnline"] != false {
		t.Fatalf("policy %v", level)
	}
	in := m2["inbounds"].([]any)[0].(map[string]any)
	if in["tag"] != WSInboundTag || in["protocol"] != "vless" || in["port"] != float64(443) || in["listen"] != "0.0.0.0" {
		t.Fatalf("inbound %v", in)
	}
	if in["settings"].(map[string]any)["decryption"] != "none" {
		t.Fatal("decryption")
	}
	for _, c := range in["settings"].(map[string]any)["clients"].([]any) {
		if c.(map[string]any)["flow"] != "" {
			t.Fatalf("a WebSocket client has a flow: %v", c)
		}
	}
	st := in["streamSettings"].(map[string]any)
	if st["network"] != "ws" || st["security"] != "tls" || st["realitySettings"] != nil {
		t.Fatalf("stream %v", st)
	}
	if st["wsSettings"].(map[string]any)["path"] != "/0123456789abcdef" {
		t.Fatalf("ws %v", st["wsSettings"])
	}
	tlsS := st["tlsSettings"].(map[string]any)
	if alpn := tlsS["alpn"].([]any); len(alpn) != 1 || alpn[0] != "http/1.1" || tlsS["minVersion"] != "1.2" {
		t.Fatalf("tls %v", tlsS)
	}
	cert := tlsS["certificates"].([]any)[0].(map[string]any)
	if cert["certificateFile"] != files.Cert || cert["keyFile"] != files.Key || len(cert) != 2 {
		t.Fatalf("certificates %v", cert)
	}
	if tag, flow := InboundOf(x); tag != WSInboundTag || flow != "" {
		t.Fatal("InboundOf")
	}
	if _, err := Render(x, users, "127.0.0.1:10085", TLSFiles{}); err == nil {
		t.Fatal("rendered without certificate files")
	}

	// The invariant check refuses hand-made variations of the ws_tls inbound.
	mutate := func(f func(in map[string]any)) []byte {
		var c map[string]any
		_ = json.Unmarshal(data, &c)
		f(c["inbounds"].([]any)[0].(map[string]any))
		out, _ := json.Marshal(c)
		return out
	}
	stream := func(in map[string]any) map[string]any { return in["streamSettings"].(map[string]any) }
	for name, bad := range map[string][]byte{
		"inline cert": mutate(func(in map[string]any) {
			c := stream(in)["tlsSettings"].(map[string]any)["certificates"].([]any)[0].(map[string]any)
			c["certificate"] = []any{"x"}
		}),
		"h2 alpn": mutate(func(in map[string]any) {
			stream(in)["tlsSettings"].(map[string]any)["alpn"] = []any{"h2", "http/1.1"}
		}),
		"tls 1.0": mutate(func(in map[string]any) { stream(in)["tlsSettings"].(map[string]any)["minVersion"] = "1.0" }),
		"relative file": mutate(func(in map[string]any) {
			stream(in)["tlsSettings"].(map[string]any)["certificates"].([]any)[0].(map[string]any)["keyFile"] = "k.key"
		}),
		"no tls": mutate(func(in map[string]any) { stream(in)["security"] = "none"; delete(stream(in), "tlsSettings") }),
		"vision on ws": mutate(func(in map[string]any) {
			in["settings"].(map[string]any)["clients"].([]any)[0].(map[string]any)["flow"] = Flow
		}),
		"ws under reality tag": mutate(func(in map[string]any) { in["tag"] = InboundTag }),
		"no path":              mutate(func(in map[string]any) { stream(in)["wsSettings"].(map[string]any)["path"] = "" }),
		"reject sni":           mutate(func(in map[string]any) { stream(in)["tlsSettings"].(map[string]any)["rejectUnknownSni"] = true }),
	} {
		if err := CheckInvariants(bad); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}

	// From the REALITY inbound to ws_tls is a restart; a user-only change of ws_tls goes live in
	// its own inbound, without a flow.
	reality := render(t, testDesired())
	if plan, _ := Decide(reality, data); plan.Kind != KindRestart {
		t.Fatalf("transport switch: %v", plan.Kind)
	}
	fewer, err := Render(x, users[:1], "127.0.0.1:10085", files)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := Decide(data, fewer)
	if err != nil || plan.Kind != KindLive || len(plan.Inbounds) != 1 || plan.Inbounds[0] != (LiveInbound{Tag: WSInboundTag}) ||
		len(plan.Remove) != 1 {
		t.Fatalf("live ws change %+v %v", plan, err)
	}
	if plan, _ := Decide(fewer, data); plan.Kind != KindLive || len(plan.Add) != 1 || plan.Inbounds[0].Tag != WSInboundTag {
		t.Fatalf("live ws add %+v", plan)
	}
	if plan, _ := Decide(render(t, testDesired()), render(t, testDesired())); plan.Kind != KindNone {
		t.Fatal("same reality config")
	}
	// A renewed certificate means other files: a restart onto them.
	renewed, err := Render(x, users, "127.0.0.1:10085", TLSFiles{Cert: "/etc/xray/tls/b.crt", Key: "/etc/xray/tls/b.key"})
	if err != nil {
		t.Fatal(err)
	}
	if plan, _ := Decide(data, renewed); plan.Kind != KindRestart {
		t.Fatalf("certificate change: %v", plan.Kind)
	}
}
