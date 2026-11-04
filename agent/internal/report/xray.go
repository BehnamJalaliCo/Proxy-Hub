package report

import (
	"regexp"
	"sort"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
)

// xrayVersion: Xray versions are three numeric parts ("26.10.31"), never address-shaped.
var xrayVersion = regexp.MustCompile(`^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$`)

// Xray is the VLESS Reality part of a report: process state and, per Reality user (a config label,
// never a person), only its cumulative byte total. Nothing here can carry an address (rule 7).
type Xray struct {
	// Enabled: this node has Xray installed and the agent manages it.
	Enabled bool `json:"enabled"`
	// Up: Xray's API answered during this cycle.
	Up      bool     `json:"up"`
	Version *string  `json:"version"`
	UptimeS *float64 `json:"uptime_s"`
	// TargetOK: the REALITY target passed the TLS 1.3 + HTTP/2 check from this node (nil: not checked).
	TargetOK   *bool      `json:"target_ok"`
	ApplyError *string    `json:"apply_error"`
	Users      []XrayUser `json:"users"`
}

// XrayUser is one Reality user's total traffic (uplink + downlink) as counted on this node.
type XrayUser struct {
	Email       string `json:"email"`
	TotalOctets uint64 `json:"total_octets"`
}

// XrayInput is what the agent knows about Xray at report time.
type XrayInput struct {
	Enabled    bool
	Up         bool
	Version    string
	UptimeS    *float64
	TargetOK   *bool
	ApplyError string
	Totals     map[string]uint64 // label -> cumulative octets
}

// BuildXray assembles the Xray section; only valid labels are reported, sorted.
func BuildXray(in XrayInput) *Xray {
	x := &Xray{Enabled: in.Enabled, Up: in.Up, TargetOK: in.TargetOK, Users: []XrayUser{}}
	if xrayVersion.MatchString(in.Version) {
		v := in.Version
		x.Version = &v
	}
	if in.Up {
		x.UptimeS = in.UptimeS
	}
	if in.ApplyError != "" {
		code := in.ApplyError
		x.ApplyError = &code
	}
	names := make([]string, 0, len(in.Totals))
	for name := range in.Totals {
		if desired.ValidXrayEmail(name) {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		x.Users = append(x.Users, XrayUser{Email: name, TotalOctets: in.Totals[name]})
	}
	return x
}
