package report

import (
	"encoding/json"
	"reflect"
	"sort"
	"testing"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/metrics"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/sysstat"
)

func keys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// The JSON shape must equal the panel's strict schema (proxyhub/agent_api/reports.py) exactly.
func TestReportShape(t *testing.T) {
	up := 12.5
	total := uint64(9)
	r := Build(Input{
		AgentVersion: "0.1.0",
		AppliedETag:  `"` + "ab" + `"`,
		ApplyOK:      true,
		TelemtUp:     true,
		Stats: metrics.Stats{
			Version: "3.5.16", UptimeS: &up, ConnectionsTotal: &total,
			Users: map[string]metrics.UserStats{
				"zed":               {CurrentConnections: 1, OctetsFromClient: 2, OctetsToClient: 3},
				"alice":             {CurrentConnections: 4, OctetsFromClient: 5, OctetsToClient: 6},
				"agent_placeholder": {CurrentConnections: 0},
			},
		},
		System: sysstat.Stats{CPUCount: 2},
	})
	data, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	if got := keys(m); !reflect.DeepEqual(got, []string{"agent_version", "applied_etag", "apply_error", "apply_ok", "format", "system", "telemt"}) {
		t.Fatalf("top-level keys = %v", got)
	}
	tm := m["telemt"].(map[string]any)
	if got := keys(tm); !reflect.DeepEqual(got, []string{"connections_bad_total", "connections_total", "handshake_timeouts_total", "up", "uptime_s", "users", "version"}) {
		t.Fatalf("telemt keys = %v", got)
	}
	users := tm["users"].([]any)
	if len(users) != 2 {
		t.Fatalf("placeholder must not be reported: %v", users)
	}
	first := users[0].(map[string]any)
	if got := keys(first); !reflect.DeepEqual(got, []string{"current_connections", "name", "total_octets"}) {
		t.Fatalf("user keys = %v", got)
	}
	if first["name"] != "alice" || first["total_octets"].(float64) != 11 {
		t.Fatalf("first user = %v", first)
	}
	if got := keys(m["system"].(map[string]any)); !reflect.DeepEqual(got, []string{"cpu_count", "load1", "mem_available_bytes", "mem_total_bytes", "uptime_s"}) {
		t.Fatalf("system keys = %v", got)
	}
}

func TestTelemtDownReportsNoCounters(t *testing.T) {
	r := Build(Input{AgentVersion: "0.1.0", ApplyOK: false, ApplyError: "service_failed"})
	data, _ := json.Marshal(r)
	var m map[string]any
	_ = json.Unmarshal(data, &m)
	tm := m["telemt"].(map[string]any)
	if tm["up"] != false || tm["users"] == nil || len(tm["users"].([]any)) != 0 {
		t.Fatalf("telemt = %v", tm)
	}
	if m["apply_error"] != "service_failed" || m["applied_etag"] != nil {
		t.Fatalf("report = %v", m)
	}
}

// The panel refuses a whole report if any string in it is shaped like an IP address (rule 7). A
// version of another shape is reported as unknown, so the rest of the report still arrives.
func TestOnlySafeVersionsAreReported(t *testing.T) {
	for v, ok := range map[string]bool{
		"3.5.16":      true,
		"10.20.300":   true,
		"3.5.16-rc1":  true,
		"":            false,
		"3.5.16.1":    false, // four numeric parts: an IPv4 shape
		"3.5":         false,
		"3.5.16-rc.1": false,
		"3.5.16+abc":  false,
		"::1":         false,
		"3.5.16-a:b":  false,
		"v3.5.16":     false,
	} {
		got := SafeVersion(v)
		if (got != nil) != ok || (got != nil && *got != v) {
			t.Errorf("SafeVersion(%q) = %v, want ok=%v", v, got, ok)
		}
	}
	r := Build(Input{AgentVersion: "0.1.0", TelemtUp: true, Stats: metrics.Stats{Version: "3.5.16.1"}})
	if r.Telemt.Version != nil {
		t.Fatalf("IP-shaped version reported: %q", *r.Telemt.Version)
	}
	r = Build(Input{AgentVersion: "0.1.0", TelemtUp: true, Stats: metrics.Stats{Version: "3.5.16"}})
	if r.Telemt.Version == nil || *r.Telemt.Version != "3.5.16" {
		t.Fatalf("version = %v", r.Telemt.Version)
	}
}
