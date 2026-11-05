package xray

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/enforce"
)

// fixedKey derives a stable X25519 private key (32 bytes, Xray's base64url form) from a label at
// run time, so no key-shaped literal sits in the repository (the secret scanner would flag it).
func fixedKey(label string) string {
	sum := sha256.Sum256([]byte("proxyhub test key: " + label))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}

var testKey = fixedKey("render")

func testDesired() *desired.Xray {
	return &desired.Xray{
		Port:        443,
		PrivateKey:  testKey,
		Target:      "dl.google.com:443",
		ServerNames: []string{"dl.google.com"},
		ShortIDs:    []string{"0123456789abcdef"},
		Users: []desired.XrayUser{
			{ID: "6a1f3a2e-1111-4c3b-9a55-0123456789ab", Email: "cfg-2@proxyhub"},
			{ID: "11111111-2222-4333-8444-555555555555", Email: "cfg-1@proxyhub"},
		},
	}
}

func render(t *testing.T, x *desired.Xray) []byte {
	t.Helper()
	users, _ := Effective(x.Users, enforce.NewUsage(), time.Now())
	data, err := Render(x, users, "127.0.0.1:10085", TLSFiles{})
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func decode(t *testing.T, data []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestRenderKeepsUsersPrivate(t *testing.T) {
	data := render(t, testDesired())
	if err := CheckInvariants(data); err != nil {
		t.Fatal(err)
	}
	m := decode(t, data)
	log := m["log"].(map[string]any)
	if log["access"] != "none" || log["dnsLog"] != false || log["maskAddress"] != "full" || log["loglevel"] != "warning" {
		t.Fatalf("log section %v", log)
	}
	level := m["policy"].(map[string]any)["levels"].(map[string]any)["0"].(map[string]any)
	if level["statsUserOnline"] != false || level["statsUserUplink"] != true || level["statsUserDownlink"] != true {
		t.Fatalf("policy %v", level)
	}
	api := m["api"].(map[string]any)
	if api["listen"] != "127.0.0.1:10085" {
		t.Fatalf("api %v", api)
	}
	in := m["inbounds"].([]any)[0].(map[string]any)
	if in["port"] != float64(443) || in["protocol"] != "vless" || in["listen"] != "0.0.0.0" {
		t.Fatalf("inbound %v", in)
	}
	// Mail ports are blocked twice: by routing, and again on the address actually dialed.
	final := m["outbounds"].([]any)[0].(map[string]any)["settings"].(map[string]any)["finalRules"].([]any)
	if mail := final[1].(map[string]any); mail["action"] != "block" || mail["network"] != "tcp" || mail["port"] != "25,465,587" {
		t.Fatalf("final rules %v", final)
	}
	if rule := m["routing"].(map[string]any)["rules"].([]any)[2].(map[string]any); rule["port"] != "25,465,587" ||
		rule["outboundTag"] != "block" {
		t.Fatalf("routing %v", rule)
	}
	reality := in["streamSettings"].(map[string]any)["realitySettings"].(map[string]any)
	if reality["target"] != "dl.google.com:443" || reality["show"] != false {
		t.Fatalf("reality %v", reality)
	}
	clients := in["settings"].(map[string]any)["clients"].([]any)
	if len(clients) != 2 || clients[0].(map[string]any)["email"] != "cfg-1@proxyhub" {
		t.Fatalf("clients are not sorted by label: %v", clients)
	}
	for _, c := range clients {
		if c.(map[string]any)["flow"] != Flow {
			t.Fatal("every client uses XTLS Vision")
		}
	}
	// Private ranges, loopback and the metadata service are blocked before and after DNS.
	text := string(data)
	for _, want := range []string{`"geoip:private"`, `"127.0.0.0/8"`, `"169.254.169.254/32"`, `"bittorrent"`, `"finalRules"`} {
		if !strings.Contains(text, want) {
			t.Errorf("config lacks %s", want)
		}
	}
	// Deterministic: the same input renders the same bytes.
	if !bytes.Equal(data, render(t, testDesired())) {
		t.Fatal("render is not deterministic")
	}
}

// Each invariant has a negative control: the check refuses the config with that one thing changed.
func TestCheckInvariantsRefusesEachBreak(t *testing.T) {
	good := render(t, testDesired())
	breaks := map[string]func(m map[string]any){
		"access log": func(m map[string]any) { m["log"].(map[string]any)["access"] = "/var/log/xray/access.log" },
		"error log":  func(m map[string]any) { m["log"].(map[string]any)["error"] = "/var/log/xray/error.log" },
		"dns log":    func(m map[string]any) { m["log"].(map[string]any)["dnsLog"] = true },
		"mask":       func(m map[string]any) { m["log"].(map[string]any)["maskAddress"] = "" },
		"debug":      func(m map[string]any) { m["log"].(map[string]any)["loglevel"] = "debug" },
		"info":       func(m map[string]any) { m["log"].(map[string]any)["loglevel"] = "info" },
		"online": func(m map[string]any) {
			m["policy"].(map[string]any)["levels"].(map[string]any)["0"].(map[string]any)["statsUserOnline"] = true
		},
		"api public": func(m map[string]any) { m["api"].(map[string]any)["listen"] = "0.0.0.0:10085" },
		"api extra": func(m map[string]any) {
			m["api"].(map[string]any)["services"] = []any{"HandlerService", "RoutingService"}
		},
		"no private block": func(m map[string]any) {
			m["routing"].(map[string]any)["rules"] = m["routing"].(map[string]any)["rules"].([]any)[:1]
		},
		"no final rule": func(m map[string]any) {
			m["outbounds"].([]any)[0].(map[string]any)["settings"] = map[string]any{"finalRules": []any{}}
		},
		"no mail final rule": func(m map[string]any) {
			s := m["outbounds"].([]any)[0].(map[string]any)["settings"].(map[string]any)
			s["finalRules"] = s["finalRules"].([]any)[:1]
		},
		"mail final rule allows": func(m map[string]any) {
			s := m["outbounds"].([]any)[0].(map[string]any)["settings"].(map[string]any)
			s["finalRules"].([]any)[1].(map[string]any)["action"] = "allow"
		},
		"no mail routing rule": func(m map[string]any) {
			m["routing"].(map[string]any)["rules"] = m["routing"].(map[string]any)["rules"].([]any)[:2]
		},
		"mail routing rule goes direct": func(m map[string]any) {
			m["routing"].(map[string]any)["rules"].([]any)[2].(map[string]any)["outboundTag"] = "direct"
		},
		"mail routing rule on other ports": func(m map[string]any) {
			m["routing"].(map[string]any)["rules"].([]any)[2].(map[string]any)["port"] = "26"
		},
		"inbound on loopback": func(m map[string]any) { m["inbounds"].([]any)[0].(map[string]any)["listen"] = "127.0.0.1" },
		"second inbound": func(m map[string]any) {
			ins := m["inbounds"].([]any)
			m["inbounds"] = append(ins, ins[0])
		},
		"show": func(m map[string]any) {
			in := m["inbounds"].([]any)[0].(map[string]any)
			in["streamSettings"].(map[string]any)["realitySettings"].(map[string]any)["show"] = true
		},
		"unknown key": func(m map[string]any) { m["metrics"] = map[string]any{"listen": "0.0.0.0:11111"} },
		"system stats": func(m map[string]any) {
			m["policy"].(map[string]any)["system"].(map[string]any)["statsInboundUplink"] = true
		},
	}
	for name, mutate := range breaks {
		m := decode(t, good)
		mutate(m)
		bad, _ := json.Marshal(m)
		err := CheckInvariants(bad)
		if !errors.Is(err, ErrRefused) {
			t.Errorf("%s: not refused (%v)", name, err)
		}
		if err != nil && strings.Contains(err.Error(), testKey) {
			t.Errorf("%s: error quotes the private key", name)
		}
	}
	if err := CheckInvariants([]byte("{")); !errors.Is(err, ErrRefused) {
		t.Error("garbage accepted")
	}
}

func TestEffectiveEnforcesExpiryAndCapLocally(t *testing.T) {
	now := time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)
	past, future := now.Add(-time.Minute), now.Add(time.Hour)
	limit := uint64(1000)
	users := []desired.XrayUser{
		{ID: "11111111-1111-4111-8111-111111111111", Email: "cfg-1@proxyhub", ExpiresAt: &past},
		{ID: "22222222-2222-4222-8222-222222222222", Email: "cfg-2@proxyhub", ExpiresAt: &future, DataCapBytes: &limit},
		{ID: "33333333-3333-4333-8333-333333333333", Email: "cfg-3@proxyhub", DataCapBytes: &limit},
	}
	usage := enforce.NewUsage()
	usage.Observe("cfg-2@proxyhub", enforce.Generation(users[1].ID), 600)
	usage.Observe("cfg-2@proxyhub", enforce.Generation(users[1].ID), 200) // Xray restarted: +200
	usage.Observe("cfg-2@proxyhub", enforce.Generation(users[1].ID), 400) // +200 = 1000
	usage.Observe("cfg-3@proxyhub", enforce.Generation(users[2].ID), 999)
	got, d := Effective(users, usage, now)
	if d.Expired != 1 || d.OverQuota != 1 {
		t.Fatalf("decision %+v", d)
	}
	if len(got) != 1 || got[0].Email != "cfg-3@proxyhub" {
		t.Fatalf("served %v", got)
	}
	// A new id under the same label starts from zero.
	users[2].ID = "44444444-4444-4444-8444-444444444444"
	usage.Observe("cfg-3@proxyhub", enforce.Generation(users[2].ID), 5)
	if usage.Total("cfg-3@proxyhub", enforce.Generation(users[2].ID)) != 5 {
		t.Fatal("generation did not reset the total")
	}
}

func TestDecide(t *testing.T) {
	base := testDesired()
	cur := render(t, base)
	if p, _ := Decide(cur, cur); p.Kind != KindNone {
		t.Fatalf("same bytes: %v", p.Kind)
	}
	if p, _ := Decide(nil, cur); p.Kind != KindRestart {
		t.Fatalf("first start: %v", p.Kind)
	}
	if p, _ := Decide([]byte("not json"), cur); p.Kind != KindRestart {
		t.Fatalf("foreign file: %v", p.Kind)
	}
	// Users only: live. One removed, one added, one with a new id (remove + add).
	next := testDesired()
	next.Users = []desired.XrayUser{
		{ID: "99999999-2222-4333-8444-555555555555", Email: "cfg-1@proxyhub"},
		{ID: "77777777-1111-4c3b-9a55-0123456789ab", Email: "cfg-3@proxyhub"},
	}
	p, err := Decide(cur, render(t, next))
	if err != nil || p.Kind != KindLive {
		t.Fatalf("users only: %v %v", p.Kind, err)
	}
	sort.Strings(p.Remove)
	if strings.Join(p.Remove, ",") != "cfg-1@proxyhub,cfg-2@proxyhub" || len(p.Add) != 2 {
		t.Fatalf("plan %+v", p)
	}
	// Anything else: restart.
	for name, change := range map[string]func(x *desired.Xray){
		"port":   func(x *desired.Xray) { x.Port = 8444 },
		"target": func(x *desired.Xray) { x.Target = "www.example.com:443"; x.ServerNames = []string{"www.example.com"} },
		"key":    func(x *desired.Xray) { x.PrivateKey = fixedKey("other") },
		"sid":    func(x *desired.Xray) { x.ShortIDs = []string{"abcd"} },
	} {
		x := testDesired()
		change(x)
		if p, _ := Decide(cur, render(t, x)); p.Kind != KindRestart {
			t.Errorf("%s: %v", name, p.Kind)
		}
	}
}

// The agent's Xray API client can name exactly three methods. Methods that return client addresses
// or online sessions exist in Xray's API (docs/10 X3); none of them may ever be named here.
func TestAPIMethodsArePinned(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "api.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	var methods []string
	ast.Inspect(file, func(n ast.Node) bool {
		if lit, ok := n.(*ast.BasicLit); ok && lit.Kind == token.STRING {
			v, _ := strconv.Unquote(lit.Value)
			if strings.HasPrefix(v, "/xray.") {
				methods = append(methods, v)
			}
		}
		return true
	})
	sort.Strings(methods)
	want := []string{
		"/xray.app.proxyman.command.HandlerService/AlterInbound",
		"/xray.app.stats.command.StatsService/GetSysStats",
		"/xray.app.stats.command.StatsService/QueryStats",
	}
	if strings.Join(methods, "\n") != strings.Join(want, "\n") {
		t.Fatalf("Xray API methods %v, want %v", methods, want)
	}
	// And no other file of the package builds an Xray method path.
	entries, _ := os.ReadDir(".")
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") || name == "api.go" {
			continue
		}
		src, _ := os.ReadFile(filepath.Clean(name))
		if bytes.Contains(src, []byte(`"/xray.`)) {
			t.Errorf("%s names an Xray API method", name)
		}
	}
	// The address-returning methods (written split, so this file does not name them either).
	for _, file := range entries {
		if !strings.HasSuffix(file.Name(), ".go") {
			continue
		}
		src, _ := os.ReadFile(filepath.Clean(file.Name()))
		for _, bad := range []string{"GetStats" + "Online", "GetAll" + "OnlineUsers", "GetUsers" + "Stats", "SourceIp" + "Block"} {
			if bytes.Contains(src, []byte(bad)) {
				t.Errorf("%s names %s", file.Name(), bad)
			}
		}
	}
}

func TestSettings(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "xray.json")
	shipped, err := os.ReadFile("../../../node-image/files/agent-xray.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, shipped, 0o600); err != nil {
		t.Fatal(err)
	}
	s, err := LoadSettings(path)
	if err != nil {
		t.Fatalf("the shipped settings file is refused: %v", err)
	}
	if s.API != "127.0.0.1:10085" || s.Config != "/etc/xray/config.json" || s.User != "xray" {
		t.Fatalf("settings %+v", s)
	}
	for name, body := range map[string]string{
		"public api":   strings.Replace(string(shipped), "127.0.0.1:10085", "0.0.0.0:10085", 1),
		"relative":     strings.Replace(string(shipped), `"/etc/xray/config.json"`, `"config.json"`, 1),
		"unknown key":  strings.Replace(string(shipped), `"user"`, `"usr"`, 1),
		"name as host": strings.Replace(string(shipped), "127.0.0.1:10085", "localhost:10085", 1),
	} {
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadSettings(path); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
