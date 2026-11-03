// Package telemt renders Telemt's config.toml from the desired state, checks it against the
// privacy and sponsorship invariants, writes it atomically and decides between hot reload and
// restart.
//
// Keys and defaults come from Telemt 3.5.16 (docs/Config_params/CONFIG_PARAMS.en.md and
// src/config): beobachten (per-IP observation) defaults to true and is forced off, log_level is
// forced to "silent", the REST API (which can list client IPs) is disabled, startup link printing
// (which would print secrets) is disabled, metrics listen on loopback only, and use_middle_proxy
// stays on because the sponsor channel needs it (decision D26).
package telemt

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
)

// Settings are node-local inputs that do not come from the panel.
type Settings struct {
	// DataDir is Telemt's working directory: quota state, caches, TLS front cache.
	DataDir string
	// MetricsListen is the loopback IP:port of Telemt's Prometheus listener.
	MetricsListen string
}

// User is a desired user after local enforcement (expiry and quota decided on the node).
type User struct {
	Name      string
	Secret    string
	Enabled   bool
	AdTag     *string
	ExpiresAt *time.Time
	DataQuota *uint64
	MaxConns  *int
}

// Render produces the config file. The result always passes CheckInvariants; Render refuses to
// return anything that does not.
func Render(t *desired.Telemt, users []User, s Settings) ([]byte, error) {
	if t == nil {
		return nil, fmt.Errorf("no telemt desired state")
	}
	if err := t.Validate(); err != nil {
		return nil, err
	}
	if len(users) == 0 {
		return nil, fmt.Errorf("render: at least one user (or the placeholder) is required")
	}
	sorted := append([]User(nil), users...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Name < sorted[j].Name })

	var b strings.Builder
	w := func(format string, args ...any) { fmt.Fprintf(&b, format, args...) }
	data := func(name string) string { return quote(filepath.Join(s.DataDir, name)) }

	w("# Managed by proxyhub-agent from the panel's desired state. Local edits are overwritten.\n")
	w("# Privacy (CLAUDE.md rule 7): beobachten off, log_level silent, API off, no link printing.\n\n")

	w("[general]\n")
	w("use_middle_proxy = true\n")
	w("beobachten = false\n")
	w("log_level = \"silent\"\n")
	w("disable_colors = true\n")
	w("config_strict = true\n")
	w("unknown_dc_file_log_enabled = false\n")
	w("quota_state_path = %s\n", data("telemt.limit.json"))
	w("proxy_secret_path = %s\n", data("proxy-secret"))
	w("proxy_config_v4_cache_path = %s\n", data("cache/proxy-config-v4.txt"))
	w("proxy_config_v6_cache_path = %s\n", data("cache/proxy-config-v6.txt"))
	if t.AdTag != nil {
		w("ad_tag = %s\n", quote(*t.AdTag))
	}
	w("\n[general.modes]\n")
	w("classic = %t\nsecure = %t\ntls = %t\n", t.Modes.Classic, t.Modes.Secure, t.Modes.TLS)
	w("\n[general.links]\n")
	w("show = []\n")
	w("\n[general.telemetry]\n")
	w("core_enabled = true\nuser_enabled = true\n")

	w("\n[network]\n")
	w("cache_public_ip_path = %s\n", data("cache/public_ip.txt"))

	w("\n[server]\n")
	w("port = %d\n", t.Ports[0])
	w("metrics_listen = %s\n", quote(s.MetricsListen))
	w("metrics_whitelist = [\"127.0.0.1/32\"]\n")
	w("\n[server.api]\n")
	w("enabled = false\n")
	for _, p := range t.Ports {
		w("\n[[server.listeners]]\nip = \"0.0.0.0\"\nport = %d\n", p)
	}

	w("\n[censorship]\n")
	w("tls_domain = %s\n", quote(t.TLSDomain))
	w("tls_front_dir = %s\n", data("tlsfront"))

	w("\n[access.users]\n")
	for _, u := range sorted {
		w("%s = %s\n", u.Name, quote(u.Secret))
	}
	section := func(name string, line func(u User) (string, bool)) {
		var lines []string
		for _, u := range sorted {
			if v, ok := line(u); ok {
				lines = append(lines, u.Name+" = "+v+"\n")
			}
		}
		if len(lines) > 0 {
			w("\n[access.%s]\n%s", name, strings.Join(lines, ""))
		}
	}
	section("user_enabled", func(u User) (string, bool) { return "false", !u.Enabled })
	section("user_ad_tags", func(u User) (string, bool) {
		if u.AdTag == nil {
			return "", false
		}
		return quote(*u.AdTag), true
	})
	section("user_expirations", func(u User) (string, bool) {
		if u.ExpiresAt == nil {
			return "", false
		}
		return quote(u.ExpiresAt.UTC().Format(time.RFC3339)), true
	})
	section("user_data_quota", func(u User) (string, bool) {
		if u.DataQuota == nil {
			return "", false
		}
		return fmt.Sprintf("%d", *u.DataQuota), true
	})
	section("user_max_tcp_conns", func(u User) (string, bool) {
		if u.MaxConns == nil {
			return "", false
		}
		return fmt.Sprintf("%d", *u.MaxConns), true
	})

	out := []byte(b.String())
	if err := CheckInvariants(out); err != nil {
		return nil, fmt.Errorf("render produced a refused config: %w", err)
	}
	return out, nil
}

// quote renders a TOML basic string.
func quote(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"' || r == '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, "\\u%04X", r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}
