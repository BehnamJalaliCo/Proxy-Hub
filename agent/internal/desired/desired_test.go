package desired

import (
	"strings"
	"testing"
)

const node = "0b7f6a3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"

func doc(telemt string) string {
	return `{"format":1,"node_id":"` + node + `","intervals":{"poll_s":15,"report_s":60},"telemt":` + telemt + `}`
}

const goodTelemt = `{"ports":[443],"tls_domain":"cdn.example.com","modes":{"classic":false,"secure":false,"tls":true},
"ad_tag":null,"use_middle_proxy":true,"beobachten":false,"log_level":"silent",
"users":[{"name":"alice","secret":"0123456789abcdef0123456789abcdef","enabled":true,"ad_tag":null,
"expires_at":"2026-12-31T23:59:59Z","data_quota_bytes":1073741824,"max_tcp_conns":100}]}`

func TestDecodeGood(t *testing.T) {
	d, err := Decode([]byte(doc(goodTelemt)), node)
	if err != nil {
		t.Fatal(err)
	}
	u := d.Telemt.Users[0]
	if u.Name != "alice" || u.ExpiresAt == nil || u.ExpiresAt.Year() != 2026 || *u.DataQuotaBytes != 1073741824 {
		t.Fatalf("decoded %+v", u)
	}
	if _, err := Decode([]byte(doc("null")), node); err != nil {
		t.Fatalf("a node without Telemt config is valid: %v", err)
	}
}

func TestDecodeRefuses(t *testing.T) {
	swap := func(old, new string) string {
		if !strings.Contains(goodTelemt, old) {
			t.Fatalf("fixture lacks %q", old)
		}
		return doc(strings.Replace(goodTelemt, old, new, 1))
	}
	cases := map[string]string{
		"beobachten on":     swap(`"beobachten":false`, `"beobachten":true`),
		"middle proxy off":  swap(`"use_middle_proxy":true`, `"use_middle_proxy":false`),
		"middle proxy gone": swap(`"use_middle_proxy":true,`, ``),
		"log level normal":  swap(`"log_level":"silent"`, `"log_level":"normal"`),
		"unknown field":     swap(`"ports":[443]`, `"ports":[443],"api":true`),
		"unknown user key":  swap(`"enabled":true`, `"enabled":true,"ips":[]`),
		"bad secret":        swap(`0123456789abcdef0123456789abcdef`, `0123`),
		"bad ad tag":        swap(`"ad_tag":null,"use`, `"ad_tag":"zz","use`),
		"user include":      swap(`"name":"alice"`, `"name":"include"`),
		"user placeholder":  swap(`"name":"alice"`, `"name":"agent_placeholder"`),
		"user upper case":   swap(`"name":"alice"`, `"name":"Alice"`),
		"metrics port":      swap(`"ports":[443]`, `"ports":[9090]`),
		"duplicate port":    swap(`"ports":[443]`, `"ports":[443,443]`),
		"no port":           swap(`"ports":[443]`, `"ports":[]`),
		"ip as domain":      swap(`"tls_domain":"cdn.example.com"`, `"tls_domain":"10.0.0.1"`),
		"no mode":           swap(`"tls":true`, `"tls":false`),
		"zero quota":        swap(`1073741824`, `0`),
		"zero conns":        swap(`"max_tcp_conns":100`, `"max_tcp_conns":0`),
		"other node":        strings.Replace(doc(goodTelemt), node, "11111111-1111-1111-1111-111111111111", 1),
		"other format":      strings.Replace(doc(goodTelemt), `"format":1`, `"format":2`, 1),
		"trailing data":     doc(goodTelemt) + `{}`,
		"poll too fast":     strings.Replace(doc(goodTelemt), `"poll_s":15`, `"poll_s":1`, 1),
	}
	for name, body := range cases {
		if _, err := Decode([]byte(body), node); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestErrorsNeverContainSecrets(t *testing.T) {
	body := doc(strings.Replace(goodTelemt, `"ad_tag":null,"use`, `"ad_tag":"zz","use`, 1))
	_, err := Decode([]byte(body), node)
	if err == nil || strings.Contains(err.Error(), "0123456789abcdef") {
		t.Fatalf("err = %v", err)
	}
}
