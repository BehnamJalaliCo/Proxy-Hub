package desired

import (
	"strings"
	"testing"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/testcert"
)

// The xhttp part of a REALITY section (agent 0.4.0 on): its own port, which may not be Xray's main
// port, a reserved port or a Telemt port, and a path of its own (never "/" alone). Never next to
// ws_tls, never with an unknown field.
func TestXHTTPPartIsValidated(t *testing.T) {
	with := func(part string) string {
		return strings.Replace(goodXray, `"port":443,`, `"port":443,"xhttp":`+part+`,`, 1)
	}
	good := with(`{"port":2083,"path":"/0123456789abcdef"}`)
	doc, err := Decode([]byte(xrayDoc(good)), node)
	if err != nil {
		t.Fatal(err)
	}
	if doc.Xray.XHTTP == nil || doc.Xray.XHTTP.Port != 2083 || doc.Xray.XHTTP.Path != "/0123456789abcdef" {
		t.Fatalf("decoded %+v", doc.Xray.XHTTP)
	}
	bad := map[string]string{
		"main port":     with(`{"port":443,"path":"/abc"}`),
		"telemt port":   with(`{"port":8443,"path":"/abc"}`),
		"api port":      with(`{"port":10085,"path":"/abc"}`),
		"metrics port":  with(`{"port":9090,"path":"/abc"}`),
		"zero port":     with(`{"port":0,"path":"/abc"}`),
		"high port":     with(`{"port":65536,"path":"/abc"}`),
		"root path":     with(`{"port":2083,"path":"/"}`),
		"no slash":      with(`{"port":2083,"path":"abc"}`),
		"query":         with(`{"port":2083,"path":"/abc?ed=2048"}`),
		"nested":        with(`{"port":2083,"path":"/a/b"}`),
		"long path":     with(`{"port":2083,"path":"/` + strings.Repeat("a", 65) + `"}`),
		"unknown field": with(`{"port":2083,"path":"/abc","mode":"packet-up"}`),
	}
	for name, x := range bad {
		if _, err := Decode([]byte(xrayDoc(x)), node); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	m, err := testcert.New("*.ipoxxo-test.example")
	if err != nil {
		t.Fatal(err)
	}
	ws := strings.Replace(wsSection(t, wsName, m), `"port":443,`, `"port":443,"xhttp":{"port":2083,"path":"/abc"},`, 1)
	if !strings.Contains(ws, `"xhttp"`) {
		t.Fatal("test setup: no xhttp part in the ws section")
	}
	if _, err := Decode([]byte(xrayDoc(ws)), node); err == nil {
		t.Error("xhttp next to ws_tls accepted")
	}
}
