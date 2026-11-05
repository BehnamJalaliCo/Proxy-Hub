// Package report builds the agent's report for POST /agent/v1/report.
//
// The shape matches the panel's strict schema exactly (unknown fields are refused there): per
// Telemt user only the current connection count and total octets, and process/system counters.
// There is no field that could carry an address (CLAUDE.md rule 7).
package report

import (
	"regexp"
	"sort"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/metrics"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/sysstat"
)

// Format is the report format version the panel accepts.
const Format = 1

// safeVersion is what a Telemt version sent to the panel may look like: three numeric parts and an
// optional short suffix without dots or colons. Such a string can never be shaped like an IPv4 or
// IPv6 address, which the panel refuses anywhere in a body (rule 7): a four-part "3.5.16.1" would
// get every report rejected, so a version of any other shape is sent as unknown instead.
var safeVersion = regexp.MustCompile(`^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}(-[0-9A-Za-z]{1,20})?$`)

// SafeVersion returns v if it may be sent as a Telemt version, and nil otherwise.
func SafeVersion(v string) *string {
	if !safeVersion.MatchString(v) {
		return nil
	}
	return &v
}

// Report is the whole body.
type Report struct {
	Format       int     `json:"format"`
	AgentVersion string  `json:"agent_version"`
	AppliedETag  *string `json:"applied_etag"`
	ApplyOK      bool    `json:"apply_ok"`
	ApplyError   *string `json:"apply_error"`
	Telemt       Telemt  `json:"telemt"`
	System       System  `json:"system"`
	Xray         *Xray   `json:"xray,omitempty"`
}

// Telemt holds process counters and per-user aggregates.
type Telemt struct {
	Up                     bool     `json:"up"`
	Version                *string  `json:"version"`
	UptimeS                *float64 `json:"uptime_s"`
	ConnectionsTotal       *uint64  `json:"connections_total"`
	ConnectionsBadTotal    *uint64  `json:"connections_bad_total"`
	HandshakeTimeoutsTotal *uint64  `json:"handshake_timeouts_total"`
	// Throughput diagnostics (agent 0.4.0 on; left out when Telemt did not report them): data from
	// the middle proxy dropped because a client's queue was full, and connections refused at
	// Telemt's max_connections. Counters since Telemt started, nothing per user or per address.
	MeRouteDropsTotal         *uint64 `json:"me_route_drops_total,omitempty"`
	AcceptPermitTimeoutsTotal *uint64 `json:"accept_permit_timeouts_total,omitempty"`
	Users                     []User  `json:"users"`
}

// User is one Telemt user's aggregate counters.
type User struct {
	Name               string `json:"name"`
	CurrentConnections uint64 `json:"current_connections"`
	TotalOctets        uint64 `json:"total_octets"`
}

// System holds whole-machine counters.
type System struct {
	UptimeS           *float64 `json:"uptime_s"`
	Load1             *float64 `json:"load1"`
	MemTotalBytes     *uint64  `json:"mem_total_bytes"`
	MemAvailableBytes *uint64  `json:"mem_available_bytes"`
	CPUCount          *int     `json:"cpu_count"`
}

// Input is what the agent knows at report time.
type Input struct {
	AgentVersion string
	AppliedETag  string
	ApplyOK      bool
	ApplyError   string
	TelemtUp     bool
	Stats        metrics.Stats
	System       sysstat.Stats
	Xray         *Xray
}

// Build assembles a report. Only users with a valid name are reported (never the placeholder).
func Build(in Input) Report {
	r := Report{
		Format:       Format,
		AgentVersion: in.AgentVersion,
		ApplyOK:      in.ApplyOK,
		Telemt:       Telemt{Up: in.TelemtUp, Users: []User{}},
		Xray:         in.Xray,
	}
	if in.AppliedETag != "" {
		etag := in.AppliedETag
		r.AppliedETag = &etag
	}
	if in.ApplyError != "" {
		code := in.ApplyError
		r.ApplyError = &code
	}
	if in.TelemtUp {
		r.Telemt.Version = SafeVersion(in.Stats.Version)
		r.Telemt.UptimeS = in.Stats.UptimeS
		r.Telemt.ConnectionsTotal = in.Stats.ConnectionsTotal
		r.Telemt.ConnectionsBadTotal = in.Stats.ConnectionsBadTotal
		r.Telemt.HandshakeTimeoutsTotal = in.Stats.HandshakeTimeoutsTotal
		r.Telemt.MeRouteDropsTotal = in.Stats.MeRouteDropsTotal
		r.Telemt.AcceptPermitTimeoutsTotal = in.Stats.AcceptPermitTimeouts
		names := make([]string, 0, len(in.Stats.Users))
		for name := range in.Stats.Users {
			if desired.ValidUserName(name) {
				names = append(names, name)
			}
		}
		sort.Strings(names)
		for _, name := range names {
			u := in.Stats.Users[name]
			r.Telemt.Users = append(r.Telemt.Users, User{
				Name:               name,
				CurrentConnections: u.CurrentConnections,
				TotalOctets:        u.TotalOctets(),
			})
		}
	}
	r.System = System{
		UptimeS:           in.System.UptimeS,
		Load1:             in.System.Load1,
		MemTotalBytes:     in.System.MemTotalBytes,
		MemAvailableBytes: in.System.MemAvailableBytes,
	}
	if in.System.CPUCount > 0 {
		n := in.System.CPUCount
		r.System.CPUCount = &n
	}
	return r
}
