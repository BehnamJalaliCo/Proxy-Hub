package desired

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/testcert"
)

const wsName = "slot3.ipoxxo-test.example"

func wsSection(t *testing.T, name string, m *testcert.Material) string {
	t.Helper()
	section := map[string]any{
		"transport": "ws_tls", "port": 443, "server_name": name, "ws_path": "/0123456789abcdef",
		"tls_cert_chain": m.ChainPEM, "tls_key": m.KeyPEM,
		"users": []any{map[string]any{
			"id": "6a1f3a2e-1111-4c3b-9a55-0123456789ab", "email": "cfg-1@proxyhub",
			"expires_at": "2026-11-07T00:00:00Z", "data_cap_bytes": 1000,
		}},
	}
	data, err := json.Marshal(section)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// The ws_tls section: the panel's wildcard certificate for the node's own name, a WebSocket path,
// the same users as REALITY. A chain and key that do not belong together, a certificate for another
// name, a bad path, or fields of the other transport are refused, and no error quotes the material.
func TestWSTLSSectionIsValidated(t *testing.T) {
	wild, err := testcert.New("*.ipoxxo-test.example")
	if err != nil {
		t.Fatal(err)
	}
	other, err := testcert.New("*.elsewhere.example")
	if err != nil {
		t.Fatal(err)
	}
	good := wsSection(t, wsName, wild)
	doc, err := Decode([]byte(xrayDoc(good)), node)
	if err != nil {
		t.Fatal(err)
	}
	x := doc.Xray
	if !x.WSTLS() || x.ServerName != wsName || x.WSPath != "/0123456789abcdef" || len(x.Users) != 1 {
		t.Fatalf("decoded %+v", x.Users)
	}
	// A test node's own name is covered by the same wildcard.
	if _, err := Decode([]byte(xrayDoc(wsSection(t, "t0a1b2c3d.ipoxxo-test.example", wild))), node); err != nil {
		t.Fatalf("test node name: %v", err)
	}
	keyLine := strings.Split(wild.KeyPEM, "\n")[1]
	swapKey := func(s string) string {
		var m map[string]any
		_ = json.Unmarshal([]byte(s), &m)
		m["tls_key"] = other.KeyPEM
		out, _ := json.Marshal(m)
		return string(out)
	}
	with := func(key string, value any) string {
		var m map[string]any
		_ = json.Unmarshal([]byte(good), &m)
		m[key] = value
		out, _ := json.Marshal(m)
		return string(out)
	}
	bad := map[string]string{
		"other name":       wsSection(t, "slot3.elsewhere.example", wild),
		"two levels":       wsSection(t, "a.slot3.ipoxxo-test.example", wild),
		"cert for another": wsSection(t, wsName, other),
		"key of another":   swapKey(good),
		"ip name":          with("server_name", "203.0.113.7"),
		"no slash":         with("ws_path", "0123456789abcdef"),
		"query in path":    with("ws_path", "/a?b=c"),
		"long path":        with("ws_path", "/"+strings.Repeat("a", 65)),
		"no chain":         with("tls_cert_chain", ""),
		"huge chain":       with("tls_cert_chain", strings.Repeat(wild.ChainPEM, 40)),
		"reality key too":  with("private_key", xrayKey),
		"reality target":   with("target", "dl.google.com:443"),
		"reality sid":      with("short_ids", []string{"0123456789abcdef"}),
		"unknown":          with("transport", "grpc_tls"),
		"api port":         with("port", 10085),
	}
	for name, section := range bad {
		_, err := Decode([]byte(xrayDoc(section)), node)
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		if strings.Contains(err.Error(), keyLine[:20]) || strings.Contains(err.Error(), "6a1f3a2e") {
			t.Errorf("%s: error quotes material: %v", name, err)
		}
	}
	// A REALITY section with ws_tls fields is refused as well.
	mixed := strings.Replace(goodXray, `"port":443`, `"port":443,"ws_path":"/0123456789abcdef"`, 1)
	if _, err := Decode([]byte(xrayDoc(mixed)), node); err == nil {
		t.Error("reality section with a ws_path accepted")
	}
	// "transport":"reality" is the same as no transport key (older panels).
	explicit := strings.Replace(goodXray, `"port":443`, `"transport":"reality","port":443`, 1)
	if doc, err := Decode([]byte(xrayDoc(explicit)), node); err != nil || doc.Xray.WSTLS() {
		t.Fatalf("explicit reality: %v", err)
	}
}
