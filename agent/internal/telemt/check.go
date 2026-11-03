package telemt

import (
	"errors"
	"fmt"
	"net"
	"regexp"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
)

// ErrRefused marks a config that breaks an invariant; such a file is never written.
var ErrRefused = errors.New("telemt config refused")

var hex32 = regexp.MustCompile(`^[0-9a-f]{32}$`)

// keyRule checks one key's value.
type keyRule func(v Value) error

// allowed is the complete set of tables and keys a rendered config may contain. Anything else
// (another Telemt key, a top-level legacy key such as `beobachten`, a [logging] section that could
// write logs to a file, the API, upstreams ...) makes the config refused.
var allowed = map[string]map[string]keyRule{
	"general": {
		"use_middle_proxy":            mustBool(true),
		"beobachten":                  mustBool(false),
		"log_level":                   mustString("silent"),
		"disable_colors":              isBool,
		"config_strict":               isBool,
		"unknown_dc_file_log_enabled": mustBool(false),
		"quota_state_path":            isString,
		"proxy_secret_path":           isString,
		"proxy_config_v4_cache_path":  isString,
		"proxy_config_v6_cache_path":  isString,
		"ad_tag":                      isHex32,
	},
	"general.modes":     {"classic": isBool, "secure": isBool, "tls": isBool},
	"general.links":     {"show": emptyArray},
	"general.telemetry": {"core_enabled": isBool, "user_enabled": isBool},
	"network":           {"cache_public_ip_path": isString},
	"server": {
		"port":              isPort,
		"metrics_listen":    loopbackHostPort,
		"metrics_whitelist": loopbackCIDRs,
	},
	"server.api":               {"enabled": mustBool(false)},
	"server.conntrack_control": {"inline_conntrack_control": mustBool(false)},
	"censorship":               {"tls_domain": isString, "tls_front_dir": isString},
	"access.users":             nil, // dynamic keys: user names, checked below
}

// Per-user maps under [access]; keys must be users from [access.users].
var userMaps = map[string]keyRule{
	"access.user_enabled":       mustBool(false),
	"access.user_ad_tags":       isHex32,
	"access.user_expirations":   isRFC3339,
	"access.user_data_quota":    positiveInt,
	"access.user_max_tcp_conns": positiveInt,
}

// Required keys: an invariant must be stated explicitly, never left to a Telemt default.
var required = map[string][]string{
	"general":       {"use_middle_proxy", "beobachten", "log_level"},
	"general.links": {"show"},
	"server":        {"metrics_listen"},
	"server.api":    {"enabled"},
}

// CheckInvariants parses data and refuses it unless every privacy and sponsorship invariant holds:
// beobachten = false, use_middle_proxy = true, log_level = "silent", API disabled, no link
// printing, metrics on loopback only, and nothing outside the known key set.
func CheckInvariants(data []byte) error {
	doc, err := ParseSubset(data)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrRefused, err)
	}
	if len(doc.Tables[""].Keys) != 0 {
		return fmt.Errorf("%w: top-level keys are not allowed (%s)", ErrRefused, doc.Tables[""].Keys[0])
	}
	for name, table := range doc.Tables {
		if name == "" || name == "access" && len(table.Keys) == 0 {
			continue
		}
		if rule, ok := userMaps[name]; ok {
			if err := checkUserMap(doc, name, table, rule); err != nil {
				return err
			}
			continue
		}
		rules, ok := allowed[name]
		if !ok {
			return fmt.Errorf("%w: table [%s] is not allowed", ErrRefused, name)
		}
		if name == "access.users" {
			if err := checkUsers(table); err != nil {
				return err
			}
			continue
		}
		for _, key := range table.Keys {
			rule, ok := rules[key]
			if !ok {
				return fmt.Errorf("%w: key %s.%s is not allowed", ErrRefused, name, key)
			}
			if err := rule(table.Values[key]); err != nil {
				return fmt.Errorf("%w: %s.%s: %v", ErrRefused, name, key, err)
			}
		}
	}
	for name, keys := range required {
		table, ok := doc.Tables[name]
		if !ok {
			return fmt.Errorf("%w: table [%s] is required", ErrRefused, name)
		}
		for _, key := range keys {
			if _, ok := table.Values[key]; !ok {
				return fmt.Errorf("%w: %s.%s must be set explicitly", ErrRefused, name, key)
			}
		}
	}
	if _, ok := doc.Tables["access.users"]; !ok {
		return fmt.Errorf("%w: [access.users] is required (Telemt would fall back to a default user)", ErrRefused)
	}
	for name, tables := range doc.Arrays {
		if name != "server.listeners" {
			return fmt.Errorf("%w: [[%s]] is not allowed", ErrRefused, name)
		}
		for _, t := range tables {
			for _, key := range t.Keys {
				var err error
				switch key {
				case "ip":
					err = anyAddress(t.Values[key])
				case "port":
					err = isPort(t.Values[key])
				default:
					err = errors.New("key not allowed")
				}
				if err != nil {
					return fmt.Errorf("%w: server.listeners.%s: %v", ErrRefused, key, err)
				}
			}
		}
	}
	return nil
}

func checkUsers(table *Table) error {
	if len(table.Keys) == 0 {
		return fmt.Errorf("%w: [access.users] is empty", ErrRefused)
	}
	for _, name := range table.Keys {
		if !desired.ValidUserName(name) && name != desired.PlaceholderUser {
			return fmt.Errorf("%w: user name %q", ErrRefused, name)
		}
		if err := isHex32(table.Values[name]); err != nil {
			return fmt.Errorf("%w: access.users.%s: secret must be 32 hex characters", ErrRefused, name)
		}
	}
	return nil
}

func checkUserMap(doc *Doc, name string, table *Table, rule keyRule) error {
	users := doc.Tables["access.users"]
	for _, key := range table.Keys {
		if users == nil {
			return fmt.Errorf("%w: %s without [access.users]", ErrRefused, name)
		}
		if _, ok := users.Values[key]; !ok {
			return fmt.Errorf("%w: %s.%s names no user", ErrRefused, name, key)
		}
		if err := rule(table.Values[key]); err != nil {
			return fmt.Errorf("%w: %s.%s: %v", ErrRefused, name, key, err)
		}
	}
	return nil
}

func mustBool(want bool) keyRule {
	return func(v Value) error {
		if v.Kind != KindBool || v.Bool != want {
			return fmt.Errorf("must be %t", want)
		}
		return nil
	}
}

func mustString(want string) keyRule {
	return func(v Value) error {
		if v.Kind != KindString || v.Str != want {
			return fmt.Errorf("must be %q", want)
		}
		return nil
	}
}

func isBool(v Value) error {
	if v.Kind != KindBool {
		return errors.New("must be a boolean")
	}
	return nil
}

func isString(v Value) error {
	if v.Kind != KindString || v.Str == "" {
		return errors.New("must be a non-empty string")
	}
	return nil
}

func isHex32(v Value) error {
	if v.Kind != KindString || !hex32.MatchString(v.Str) {
		return errors.New("must be 32 lower-case hex characters")
	}
	return nil
}

func isPort(v Value) error {
	if v.Kind != KindInt || v.Int < 1 || v.Int > 65535 {
		return errors.New("must be a port number")
	}
	return nil
}

func positiveInt(v Value) error {
	if v.Kind != KindInt || v.Int <= 0 {
		return errors.New("must be a positive integer")
	}
	return nil
}

func isRFC3339(v Value) error {
	if v.Kind != KindString {
		return errors.New("must be an RFC 3339 string")
	}
	if _, err := time.Parse(time.RFC3339, v.Str); err != nil {
		return errors.New("must be an RFC 3339 string")
	}
	return nil
}

func emptyArray(v Value) error {
	if v.Kind != KindArray || len(v.Items) != 0 {
		return errors.New("must be [] (Telemt would print proxy links, which contain secrets)")
	}
	return nil
}

func loopbackHostPort(v Value) error {
	if v.Kind != KindString {
		return errors.New("must be IP:port")
	}
	host, _, err := net.SplitHostPort(v.Str)
	ip := net.ParseIP(host)
	if err != nil || ip == nil || !ip.IsLoopback() {
		return errors.New("must be a loopback IP:port")
	}
	return nil
}

func loopbackCIDRs(v Value) error {
	if v.Kind != KindArray || len(v.Items) == 0 {
		return errors.New("must be a non-empty list")
	}
	for _, item := range v.Items {
		_, network, err := net.ParseCIDR(item.Str)
		if item.Kind != KindString || err != nil || !network.IP.IsLoopback() {
			return errors.New("only loopback networks may scrape metrics")
		}
	}
	return nil
}

func anyAddress(v Value) error {
	if v.Kind != KindString || (v.Str != "0.0.0.0" && v.Str != "::") {
		return errors.New("listeners bind the wildcard address only")
	}
	return nil
}
