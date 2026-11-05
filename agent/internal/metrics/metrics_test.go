package metrics

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func loadFixture(t *testing.T) []byte {
	t.Helper()
	data, err := os.ReadFile("testdata/telemt-3.5.16-metrics.txt")
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestParseTelemtExposition(t *testing.T) {
	samples, err := Parse(strings.NewReader(string(loadFixture(t))))
	if err != nil {
		t.Fatal(err)
	}
	st := Extract(samples)
	if st.Version != "3.5.16" {
		t.Fatalf("version = %q", st.Version)
	}
	if st.UptimeS == nil || *st.UptimeS != 3725.4 {
		t.Fatalf("uptime = %v", st.UptimeS)
	}
	if st.ConnectionsTotal == nil || *st.ConnectionsTotal != 1532 ||
		st.ConnectionsBadTotal == nil || *st.ConnectionsBadTotal != 17 ||
		st.HandshakeTimeoutsTotal == nil || *st.HandshakeTimeoutsTotal != 4 {
		t.Fatalf("process counters = %+v", st)
	}
	alice := st.Users["alice"]
	if alice.CurrentConnections != 37 || alice.OctetsFromClient != 1048576 || alice.OctetsToClient != 52428800 {
		t.Fatalf("alice = %+v", alice)
	}
	if alice.TotalOctets() != 1048576+52428800 {
		t.Fatalf("alice total = %d", alice.TotalOctets())
	}
	if b := st.Users["sponsor_b"]; b.CurrentConnections != 0 || b.TotalOctets() != 20480+409600 {
		t.Fatalf("sponsor_b = %+v", b)
	}
	if len(st.Users) != 2 {
		t.Fatalf("users = %v", st.Users)
	}
}

// Rule 7: anything derived from client addresses (the unique-IP gauges, the IP tracker) is dropped
// while parsing; it never reaches the agent's data.
func TestAddressDerivedSeriesAreDropped(t *testing.T) {
	samples, err := Parse(strings.NewReader(string(loadFixture(t))))
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range samples {
		if strings.Contains(s.Name, "_ip") {
			t.Fatalf("address-derived series kept: %s", s.Name)
		}
		if !kept[s.Name] {
			t.Fatalf("series outside the allow-list kept: %s", s.Name)
		}
	}
}

func TestParseRejectsMalformedSample(t *testing.T) {
	for _, bad := range []string{
		"telemt_connections_total\n",
		"telemt_connections_total abc\n",
		"telemt_user_connections_current{user=\"alice} 1\n",
		"telemt_user_connections_current{user=alice} 1\n",
		"telemt_connections_total 1 2 3\n",
	} {
		if _, err := Parse(strings.NewReader(bad)); err == nil {
			t.Errorf("accepted %q", bad)
		}
	}
}

func TestExtractIgnoresUnexpectedLabelsAndNegativeValues(t *testing.T) {
	samples, err := Parse(strings.NewReader(strings.Join([]string{
		`telemt_user_connections_current{user="alice",extra="x"} 5`,
		`telemt_user_connections_current{} 5`,
		`telemt_user_octets_to_client_total{user="bob"} -1`,
		`telemt_connections_total{class="x"} 9`,
		`telemt_user_connections_current{user="carol"} 3 1700000000000`,
	}, "\n")))
	if err != nil {
		t.Fatal(err)
	}
	st := Extract(samples)
	if _, ok := st.Users["alice"]; ok {
		t.Fatal("series with an extra label used")
	}
	if _, ok := st.Users["bob"]; ok {
		t.Fatal("negative counter used")
	}
	if st.ConnectionsTotal != nil {
		t.Fatal("labelled process counter used")
	}
	if st.Users["carol"].CurrentConnections != 3 {
		t.Fatal("sample with timestamp not parsed")
	}
}

// The two throughput diagnostics are read as plain process counters (Telemt 3.5.16 renders them
// unlabelled: src/metrics/render/me_lifecycle.rs and connections.rs); labelled or negative series
// are ignored, and without them the fields stay unknown.
func TestThroughputDiagnostics(t *testing.T) {
	samples, err := Parse(strings.NewReader(strings.Join([]string{
		"# TYPE telemt_me_route_drop_queue_full_total counter",
		"telemt_me_route_drop_queue_full_total 42",
		"telemt_accept_permit_timeout_total 0",
		`telemt_me_route_drop_queue_full_profile_total{profile="x"} 7`,
	}, "\n")))
	if err != nil {
		t.Fatal(err)
	}
	st := Extract(samples)
	if st.MeRouteDropsTotal == nil || *st.MeRouteDropsTotal != 42 || st.AcceptPermitTimeouts == nil || *st.AcceptPermitTimeouts != 0 {
		t.Fatalf("diagnostics %v %v", st.MeRouteDropsTotal, st.AcceptPermitTimeouts)
	}
	samples, _ = Parse(strings.NewReader(`telemt_me_route_drop_queue_full_total{dc="2"} 1` + "\ntelemt_accept_permit_timeout_total -3\n"))
	if st := Extract(samples); st.MeRouteDropsTotal != nil || st.AcceptPermitTimeouts != nil {
		t.Fatal("a labelled or negative diagnostic was used")
	}
	if st := Extract(nil); st.MeRouteDropsTotal != nil || st.AcceptPermitTimeouts != nil {
		t.Fatal("diagnostics invented without samples")
	}
}

func TestLabelEscapes(t *testing.T) {
	labels, rest, err := parseLabels(`{user="a\"b\\c\nd",x="y",} 1`)
	if err != nil {
		t.Fatal(err)
	}
	if labels["user"] != "a\"b\\c\nd" || labels["x"] != "y" || strings.TrimSpace(rest) != "1" {
		t.Fatalf("labels = %q rest = %q", labels, rest)
	}
}

// A real HTTP round trip against an in-process server serving the fixture.
func TestScrapeOverHTTP(t *testing.T) {
	body := loadFixture(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/metrics" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		_, _ = w.Write(body)
	}))
	defer srv.Close()
	st, err := Scrape(context.Background(), srv.Client(), srv.URL+"/metrics")
	if err != nil {
		t.Fatal(err)
	}
	if st.Users["alice"].CurrentConnections != 37 {
		t.Fatalf("scrape = %+v", st)
	}
	if _, err := Scrape(context.Background(), srv.Client(), srv.URL+"/other"); err == nil {
		t.Fatal("non-200 accepted")
	}
}
