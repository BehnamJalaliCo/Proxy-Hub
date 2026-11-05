package report

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestBuildXray(t *testing.T) {
	uptime := 12.0
	ok := true
	x := BuildXray(XrayInput{
		Enabled: true, Up: true, Version: "26.10.31", UptimeS: &uptime, TargetOK: &ok,
		CheckedTarget: "dl.google.com:443", CheckedServerName: "dl.google.com",
		Totals: map[string]uint64{"cfg-2@proxyhub": 7, "cfg-1@proxyhub": 5, "1.2.3.4@x": 1, "bad>>>name@x": 2},
	})
	data, _ := json.Marshal(x)
	want := `{"enabled":true,"up":true,"version":"26.10.31","uptime_s":12,"target_ok":true,` +
		`"checked_target":"dl.google.com:443","checked_server_name":"dl.google.com","apply_error":null,` +
		`"users":[{"email":"cfg-1@proxyhub","total_octets":5},{"email":"cfg-2@proxyhub","total_octets":7}]}`
	if string(data) != want {
		t.Fatalf("got  %s\nwant %s", data, want)
	}
	// A version of another shape is never sent (it could look like an address); down: no uptime.
	x = BuildXray(XrayInput{Enabled: true, Version: "26.10.31.1", UptimeS: &uptime, ApplyError: "xray_config_refused"})
	data, _ = json.Marshal(x)
	if strings.Contains(string(data), "26.10") || strings.Contains(string(data), `"uptime_s":12`) ||
		!strings.Contains(string(data), `"apply_error":"xray_config_refused"`) {
		t.Fatalf("got %s", data)
	}
	// The XHTTP inbound's port only while Xray is up, and only a real port.
	data, _ = json.Marshal(BuildXray(XrayInput{Enabled: true, Up: true, XHTTPPort: 2083}))
	if !strings.Contains(string(data), `"apply_error":null,"xhttp_port":2083,"users":[]`) {
		t.Fatalf("got %s", data)
	}
	for _, in := range []XrayInput{{Enabled: true, XHTTPPort: 2083}, {Enabled: true, Up: true, XHTTPPort: 70000}, {Up: true}} {
		if data, _ := json.Marshal(BuildXray(in)); strings.Contains(string(data), "xhttp") {
			t.Fatalf("got %s", data)
		}
	}
	// A node without Xray still says so (the panel learns this agent understands Xray).
	data, _ = json.Marshal(BuildXray(XrayInput{}))
	if string(data) != `{"enabled":false,"up":false,"version":null,"uptime_s":null,"target_ok":null,`+
		`"checked_target":null,"checked_server_name":null,"apply_error":null,"users":[]}` {
		t.Fatalf("got %s", data)
	}
}

// A target check is only reported with the target and server name it was about, and only when both
// are DNS names: never an address, never a result the panel could pin on the wrong target.
func TestBuildXrayTargetCheckNeedsWhatItWasAbout(t *testing.T) {
	failed := false
	for name, in := range map[string]XrayInput{
		"no target":          {TargetOK: &failed, CheckedServerName: "dl.google.com"},
		"no server name":     {TargetOK: &failed, CheckedTarget: "dl.google.com:443"},
		"ip target":          {TargetOK: &failed, CheckedTarget: "142.250.0.1:443", CheckedServerName: "dl.google.com"},
		"ip server name":     {TargetOK: &failed, CheckedTarget: "dl.google.com:443", CheckedServerName: "142.250.0.1"},
		"no port":            {TargetOK: &failed, CheckedTarget: "dl.google.com", CheckedServerName: "dl.google.com"},
		"port out of range":  {TargetOK: &failed, CheckedTarget: "dl.google.com:70000", CheckedServerName: "dl.google.com"},
		"ipv6 target":        {TargetOK: &failed, CheckedTarget: "[2001:db8::1]:443", CheckedServerName: "dl.google.com"},
		"target, not result": {CheckedTarget: "dl.google.com:443", CheckedServerName: "dl.google.com"},
	} {
		data, _ := json.Marshal(BuildXray(in))
		if !strings.Contains(string(data), `"target_ok":null,"checked_target":null,"checked_server_name":null`) {
			t.Fatalf("%s: got %s", name, data)
		}
	}
	data, _ := json.Marshal(BuildXray(XrayInput{
		TargetOK: &failed, CheckedTarget: "www.example.org:8443", CheckedServerName: "www.example.org",
	}))
	if !strings.Contains(string(data), `"target_ok":false,"checked_target":"www.example.org:8443","checked_server_name":"www.example.org"`) {
		t.Fatalf("got %s", data)
	}
}
