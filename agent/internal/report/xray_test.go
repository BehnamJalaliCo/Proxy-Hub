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
		Totals: map[string]uint64{"cfg-2@proxyhub": 7, "cfg-1@proxyhub": 5, "1.2.3.4@x": 1, "bad>>>name@x": 2},
	})
	data, _ := json.Marshal(x)
	want := `{"enabled":true,"up":true,"version":"26.10.31","uptime_s":12,"target_ok":true,"apply_error":null,` +
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
	// A node without Xray still says so (the panel learns this agent understands Xray).
	data, _ = json.Marshal(BuildXray(XrayInput{}))
	if string(data) != `{"enabled":false,"up":false,"version":null,"uptime_s":null,"target_ok":null,"apply_error":null,"users":[]}` {
		t.Fatalf("got %s", data)
	}
}
