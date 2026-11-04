package desired

import (
	"crypto/sha256"
	"encoding/base64"
	"strings"
	"testing"
)

// xrayKey is a stable X25519 private key derived at run time (no key-shaped literal in the repo).
var xrayKey = func() string {
	sum := sha256.Sum256([]byte("proxyhub test key: desired"))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}()

func xrayDoc(xray string) string {
	return `{"format":1,"node_id":"` + node + `","intervals":{"poll_s":15,"report_s":60},
"telemt":{"ports":[8443],"tls_domain":"cdn.example.com","modes":{"classic":false,"secure":false,"tls":true},
"ad_tag":null,"use_middle_proxy":true,"beobachten":false,"log_level":"silent","users":[]},
"xray":` + xray + `}`
}

var goodXray = `{"port":443,"private_key":"` + xrayKey + `","target":"dl.google.com:443",
"server_names":["dl.google.com"],"short_ids":["0123456789abcdef"],
"users":[{"id":"6a1f3a2e-1111-4c3b-9a55-0123456789ab","email":"cfg-1@proxyhub","expires_at":"2026-11-05T00:00:00Z","data_cap_bytes":1000}]}`

func TestXraySectionIsValidated(t *testing.T) {
	doc, err := Decode([]byte(xrayDoc(goodXray)), node)
	if err != nil {
		t.Fatal(err)
	}
	if doc.Xray == nil || doc.Xray.Users[0].Email != "cfg-1@proxyhub" || *doc.Xray.Users[0].DataCapBytes != 1000 {
		t.Fatalf("decoded %+v", doc.Xray)
	}
	// Without the key the document is a Telemt-only one, as from an older panel.
	if doc, err := Decode([]byte(strings.Replace(xrayDoc("null"), `,"xray":null`, "", 1)), node); err != nil || doc.Xray != nil {
		t.Fatalf("no xray section: %v", err)
	}
	bad := map[string]string{
		"telemt port":    strings.Replace(goodXray, `"port":443`, `"port":8443`, 1),
		"api port":       strings.Replace(goodXray, `"port":443`, `"port":10085`, 1),
		"metrics port":   strings.Replace(goodXray, `"port":443`, `"port":9090`, 1),
		"short key":      strings.Replace(goodXray, xrayKey, xrayKey[:42], 1),
		"padded key":     strings.Replace(goodXray, xrayKey, xrayKey[:42]+"=", 1),
		"ip target":      strings.Replace(goodXray, "dl.google.com:443", "1.2.3.4:443", 1),
		"no port target": strings.Replace(goodXray, "dl.google.com:443", "dl.google.com", 1),
		"no names":       strings.Replace(goodXray, `["dl.google.com"]`, `[]`, 1),
		"odd sid":        strings.Replace(goodXray, "0123456789abcdef", "abc", 1),
		"empty sid":      strings.Replace(goodXray, `"0123456789abcdef"`, `""`, 1),
		"long sid":       strings.Replace(goodXray, "0123456789abcdef", "0123456789abcdef00", 1),
		"upper uuid":     strings.Replace(goodXray, "6a1f3a2e", "6A1F3A2E", 1),
		"stats name":     strings.Replace(goodXray, "cfg-1@proxyhub", "a>>>b@proxyhub", 1),
		"ip label":       strings.Replace(goodXray, "cfg-1@proxyhub", "1.2.3.4@proxyhub", 1),
		"zero cap":       strings.Replace(goodXray, `"data_cap_bytes":1000`, `"data_cap_bytes":0`, 1),
		"unknown field":  strings.Replace(goodXray, `"port":443`, `"port":443,"log":"debug"`, 1),
	}
	for name, x := range bad {
		_, err := Decode([]byte(xrayDoc(x)), node)
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		if strings.Contains(err.Error(), xrayKey[:20]) || strings.Contains(err.Error(), "6a1f3a2e") {
			t.Errorf("%s: error quotes a secret: %v", name, err)
		}
	}
	dup := strings.Replace(goodXray, `"users":[{`, `"users":[{"id":"6a1f3a2e-1111-4c3b-9a55-0123456789ab","email":"cfg-2@proxyhub","expires_at":null,"data_cap_bytes":null},{`, 1)
	if _, err := Decode([]byte(xrayDoc(dup)), node); err == nil {
		t.Error("duplicate user id accepted")
	}
	telemtOnAPI := strings.Replace(xrayDoc(goodXray), `"ports":[8443]`, `"ports":[10085]`, 1)
	if _, err := Decode([]byte(telemtOnAPI), node); err == nil {
		t.Error("Telemt on Xray's API port accepted")
	}
}
