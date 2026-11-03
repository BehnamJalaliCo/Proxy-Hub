// Package report builds the agent's report for POST /agent/v1/report.
//
// The shape matches the panel's strict schema exactly (unknown fields are refused there): per
// Telemt user only the current connection count and total octets, and process/system counters.
// There is no field that could carry an address (CLAUDE.md rule 7).
package report

import (
	"sort"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/metrics"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/sysstat"
)

// Format is the report format version the panel accepts.
const Format = 1

// Report is the whole body.
type Report struct {
	Format       int     `json:"format"`
	AgentVersion string  `json:"agent_version"`
	AppliedETag  *string `json:"applied_etag"`
	ApplyOK      bool    `json:"apply_ok"`
	ApplyError   *string `json:"apply_error"`
	Telemt       Telemt  `json:"telemt"`
	System       System  `json:"system"`
}

// Telemt holds process counters and per-user aggregates.
type Telemt struct {
	Up                     bool     `json:"up"`
	Version                *string  `json:"version"`
	UptimeS                *float64 `json:"uptime_s"`
	ConnectionsTotal       *uint64  `json:"connections_total"`
	ConnectionsBadTotal    *uint64  `json:"connections_bad_total"`
	HandshakeTimeoutsTotal *uint64  `json:"handshake_timeouts_total"`
	Users                  []User   `json:"users"`
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
}

// Build assembles a report. Only users with a valid name are reported (never the placeholder).
func Build(in Input) Report {
	r := Report{
		Format:       Format,
		AgentVersion: in.AgentVersion,
		ApplyOK:      in.ApplyOK,
		Telemt:       Telemt{Up: in.TelemtUp, Users: []User{}},
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
		if in.Stats.Version != "" {
			v := in.Stats.Version
			r.Telemt.Version = &v
		}
		r.Telemt.UptimeS = in.Stats.UptimeS
		r.Telemt.ConnectionsTotal = in.Stats.ConnectionsTotal
		r.Telemt.ConnectionsBadTotal = in.Stats.ConnectionsBadTotal
		r.Telemt.HandshakeTimeoutsTotal = in.Stats.HandshakeTimeoutsTotal
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
