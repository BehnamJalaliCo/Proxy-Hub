package telemt

import (
	"bytes"
	"errors"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
)

var update = flag.Bool("update", false, "rewrite testdata/telemt.golden.toml")

const (
	secretA = "0123456789abcdef0123456789abcdef"
	secretB = "fedcba9876543210fedcba9876543210"
	tagMain = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	tagCust = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

func ptr[T any](v T) *T { return &v }

func sampleTelemt() *desired.Telemt {
	return &desired.Telemt{
		Ports:          []int{443, 8443},
		TLSDomain:      "cdn.example.com",
		Modes:          desired.Modes{TLS: true},
		AdTag:          ptr(tagMain),
		UseMiddleProxy: true,
		Beobachten:     false,
		LogLevel:       "silent",
	}
}

func sampleUsers() []User {
	expires := time.Date(2026, 12, 31, 23, 59, 59, 0, time.UTC)
	return []User{
		{Name: "sponsor_b", Secret: secretB, Enabled: false, AdTag: ptr(tagCust)},
		{Name: "alice", Secret: secretA, Enabled: true, ExpiresAt: &expires, DataQuota: ptr(uint64(107374182400)), MaxConns: ptr(500)},
	}
}

var settings = Settings{DataDir: "/var/lib/telemt", MetricsListen: "127.0.0.1:9090"}

func render(t *testing.T) []byte {
	t.Helper()
	out, err := Render(sampleTelemt(), sampleUsers(), settings)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func TestRenderMatchesGolden(t *testing.T) {
	out := render(t)
	golden := filepath.Join("testdata", "telemt.golden.toml")
	if *update {
		if err := os.WriteFile(golden, out, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(golden)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(out, want) {
		t.Fatalf("render differs from golden:\n%s", out)
	}
}

func TestRenderIsDeterministicAndPassesInvariants(t *testing.T) {
	a, b := render(t), render(t)
	if !bytes.Equal(a, b) {
		t.Fatal("render is not deterministic")
	}
	if err := CheckInvariants(a); err != nil {
		t.Fatal(err)
	}
	doc, err := ParseSubset(a)
	if err != nil {
		t.Fatal(err)
	}
	g := doc.Tables["general"].Values
	if !g["use_middle_proxy"].Bool || g["beobachten"].Bool || g["log_level"].Str != "silent" {
		t.Fatal("invariants not rendered explicitly")
	}
	if doc.Tables["server.api"].Values["enabled"].Bool {
		t.Fatal("telemt API must be disabled")
	}
	if doc.Tables["access.users"].Values["alice"].Str != secretA {
		t.Fatal("user secret missing")
	}
	if doc.Tables["access.user_enabled"].Values["sponsor_b"].Bool {
		t.Fatal("disabled user rendered enabled")
	}
	if _, ok := doc.Tables["access.user_enabled"].Values["alice"]; ok {
		t.Fatal("enabled users need no override")
	}
	if doc.Tables["access.user_ad_tags"].Values["sponsor_b"].Str != tagCust {
		t.Fatal("per-user sponsor tag missing")
	}
	if doc.Tables["access.user_expirations"].Values["alice"].Str != "2026-12-31T23:59:59Z" {
		t.Fatal("expiry missing")
	}
	if len(doc.Arrays["server.listeners"]) != 2 {
		t.Fatal("one listener per port expected")
	}
}

// The desired state itself is refused when it asks for anything that breaks an invariant.
func TestRenderRefusesUnsafeDesiredState(t *testing.T) {
	cases := map[string]func(*desired.Telemt){
		"beobachten on":      func(d *desired.Telemt) { d.Beobachten = true },
		"middle proxy off":   func(d *desired.Telemt) { d.UseMiddleProxy = false },
		"log level normal":   func(d *desired.Telemt) { d.LogLevel = "normal" },
		"metrics port taken": func(d *desired.Telemt) { d.Ports = []int{9090} },
		"bad domain":         func(d *desired.Telemt) { d.TLSDomain = "1.2.3.4" },
	}
	for name, mutate := range cases {
		d := sampleTelemt()
		mutate(d)
		if _, err := Render(d, sampleUsers(), settings); err == nil {
			t.Errorf("%s: rendered", name)
		}
	}
	if _, err := Render(sampleTelemt(), nil, settings); err == nil {
		t.Error("rendered without any user (Telemt would add a default user)")
	}
	if _, err := Render(sampleTelemt(), []User{{Name: "include", Secret: secretA, Enabled: true}}, settings); err == nil {
		t.Error("rendered a user named include (Telemt include directive)")
	}
}

// Every way a written file could switch on observation, logging, the API or links is refused.
func TestCheckInvariantsRefusesTampering(t *testing.T) {
	base := string(render(t))
	replace := func(old, new string) string {
		if !strings.Contains(base, old) {
			t.Fatalf("fixture lacks %q", old)
		}
		return strings.Replace(base, old, new, 1)
	}
	cases := map[string]string{
		"beobachten true":         replace("beobachten = false", "beobachten = true"),
		"beobachten missing":      replace("beobachten = false\n", ""),
		"middle proxy off":        replace("use_middle_proxy = true", "use_middle_proxy = false"),
		"middle proxy missing":    replace("use_middle_proxy = true\n", ""),
		"log level normal":        replace(`log_level = "silent"`, `log_level = "normal"`),
		"log level missing":       replace("log_level = \"silent\"\n", ""),
		"api enabled":             replace("[server.api]\nenabled = false", "[server.api]\nenabled = true"),
		"api listen":              replace("[server.api]\nenabled = false", "[server.api]\nenabled = false\nlisten = \"0.0.0.0:9091\""),
		"links shown":             replace("show = []", `show = "*"`),
		"metrics public":          replace(`metrics_listen = "127.0.0.1:9090"`, `metrics_listen = "0.0.0.0:9090"`),
		"metrics whitelist wide":  replace(`metrics_whitelist = ["127.0.0.1/32"]`, `metrics_whitelist = ["0.0.0.0/0"]`),
		"top-level beobachten":    "beobachten = true\n" + base,
		"top-level dotted key":    "general.beobachten = true\n" + base,
		"include directive":       "include = \"/tmp/x.toml\"\n" + base,
		"include indented":        replace("[general]\n", "[general]\n  include   = \"x.toml\"\n"),
		"logging to file":         base + "\n[logging]\ndestination = \"file\"\npath = \"/var/log/telemt.log\"\n",
		"duplicate key":           replace("beobachten = false\n", "beobachten = false\nbeobachten = true\n"),
		"duplicate table":         base + "\n[general]\nfast_mode = true\n",
		"unknown key":             replace("[general]\n", "[general]\nbeobachten_minutes = 10\n"),
		"unknown table":           base + "\n[[upstreams]]\ntype = \"direct\"\n",
		"multiline string":        replace(`tls_domain = "cdn.example.com"`, `tls_domain = """cdn.example.com"""`),
		"inline table":            replace(`tls_domain = "cdn.example.com"`, `tls_domain = { a = 1 }`),
		"quoted key":              replace("alice = \""+secretA, "\"alice\" = \""+secretA),
		"unknown user override":   base + "\n[access.user_max_unique_ips]\nalice = 3\n",
		"override for no user":    replace("[access.user_enabled]\n", "[access.user_enabled]\nmallory = false\n"),
		"user enabled true":       replace("sponsor_b = false", "sponsor_b = true"),
		"bad secret":              replace(secretA+"\"", "0000\""),
		"listener on loopback":    replace("ip = \"0.0.0.0\"", "ip = \"127.0.0.1\""),
		"listener extra key":      replace("ip = \"0.0.0.0\"\n", "ip = \"0.0.0.0\"\nproxy_protocol = true\n"),
		"carriage return":         replace("[general]\n", "[general]\r\n"),
		"key clashes with table":  replace("[general]\n", "[general]\nmodes = 1\n"),
		"unterminated string":     replace(`tls_domain = "cdn.example.com"`, `tls_domain = "cdn.example.com`),
		"trailing text":           replace("beobachten = false", "beobachten = false true"),
		"control char in string":  replace(`tls_domain = "cdn.example.com"`, "tls_domain = \"cdn\x01example.com\""),
		"array of tables clash":   base + "\n[censorship.x]\n[[censorship.x]]\n",
		"empty users":             emptyUsers(t, base),
		"general.links missing":   replace("[general.links]\nshow = []\n", ""),
		"server.api missing":      replace("[server.api]\nenabled = false\n", ""),
		"metrics listen missing":  replace("metrics_listen = \"127.0.0.1:9090\"\n", ""),
		"unknown dc file logging": replace("unknown_dc_file_log_enabled = false", "unknown_dc_file_log_enabled = true"),
	}
	for name, data := range cases {
		err := CheckInvariants([]byte(data))
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		if !errors.Is(err, ErrRefused) {
			t.Errorf("%s: error is not ErrRefused: %v", name, err)
		}
	}
}

func emptyUsers(t *testing.T, base string) string {
	t.Helper()
	start := strings.Index(base, "[access.users]\n")
	end := strings.Index(base, "\n[access.user_enabled]")
	if start < 0 || end < 0 {
		t.Fatal("fixture layout changed")
	}
	return base[:start] + "[access.users]\n" + base[end:]
}

func TestWriteConfigIsAtomicPrivateAndRefusesBadData(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "telemt.toml")
	good := render(t)
	if err := WriteConfig(path, good, fsutil.KeepOwner); err != nil {
		t.Fatal(err)
	}
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v, want 0600", st.Mode().Perm())
	}
	bad := bytes.Replace(good, []byte("beobachten = false"), []byte("beobachten = true"), 1)
	if err := WriteConfig(path, bad, fsutil.KeepOwner); !errors.Is(err, ErrRefused) {
		t.Fatalf("bad config not refused: %v", err)
	}
	now, _ := os.ReadFile(path)
	if !bytes.Equal(now, good) {
		t.Fatal("refused write changed the file")
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Fatalf("temporary files left behind: %v", entries)
	}
}

func TestDecide(t *testing.T) {
	base := render(t)
	other := func(mutate func(d *desired.Telemt, u []User)) []byte {
		d, u := sampleTelemt(), sampleUsers()
		mutate(d, u)
		out, err := Render(d, u, settings)
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	cases := []struct {
		name string
		cur  []byte
		next []byte
		want Action
	}{
		{"unchanged", base, base, ActionNone},
		{"first start", nil, base, ActionRestart},
		{"foreign file", []byte("garbage = [\n"), base, ActionRestart},
		{"secret changed", base, other(func(_ *desired.Telemt, u []User) { u[1].Secret = secretB }), ActionReload},
		{"user disabled", base, other(func(_ *desired.Telemt, u []User) { u[1].Enabled = false }), ActionReload},
		{"sponsor tag switched", base, other(func(d *desired.Telemt, u []User) { d.AdTag = ptr(tagCust); u[0].AdTag = nil }), ActionReload},
		{"quota changed", base, other(func(_ *desired.Telemt, u []User) { u[1].DataQuota = ptr(uint64(1)) }), ActionReload},
		{"port changed", base, other(func(d *desired.Telemt, _ []User) { d.Ports = []int{443} }), ActionRestart},
		{"tls domain changed", base, other(func(d *desired.Telemt, _ []User) { d.TLSDomain = "www.example.org" }), ActionRestart},
		{"mode changed", base, other(func(d *desired.Telemt, _ []User) { d.Modes.Secure = true }), ActionRestart},
	}
	for _, c := range cases {
		got, err := Decide(c.cur, c.next)
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		if got != c.want {
			t.Errorf("%s: got %s, want %s", c.name, got, c.want)
		}
	}
}

func TestServiceRunsConfiguredCommands(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "restarted")
	s := Service{
		ReloadCommand:  []string{"false"},
		RestartCommand: []string{"touch", marker},
	}
	if err := s.Do(t.Context(), ActionNone); err != nil {
		t.Fatal(err)
	}
	if err := s.Do(t.Context(), ActionRestart); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("restart command did not run")
	}
	if err := s.Do(t.Context(), ActionReload); err == nil {
		t.Fatal("failing reload command not reported")
	}
}
