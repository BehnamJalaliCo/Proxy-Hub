// Package enforce decides on the node which users may still be served, so expiry and data quota
// hold even while the panel is unreachable (docs/09 R10). Telemt enforces user_expirations and
// user_data_quota itself as well; this is the second, independent lock: an expired or exhausted
// user is rendered with user_enabled = false.
//
// Usage is counted from Telemt's per-user octet counters, which restart from zero when Telemt
// restarts; the tracker adds up the increases so a restart does not hand out a fresh quota.
package enforce

import (
	"crypto/sha256"
	"encoding/hex"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/telemt"
)

// UserUsage is the cumulative traffic of one user (one secret).
type UserUsage struct {
	// Generation identifies the secret (a hash prefix, never the secret): a new secret under an
	// old name starts from zero.
	Generation string `json:"gen"`
	LastRaw    uint64 `json:"last_raw"`
	Total      uint64 `json:"total"`
}

// Usage is the per-user tracker, persisted by the agent between runs.
type Usage struct {
	Users map[string]*UserUsage `json:"users"`
}

// NewUsage returns an empty tracker.
func NewUsage() *Usage { return &Usage{Users: map[string]*UserUsage{}} }

// Generation derives the non-reversible secret identifier stored in the usage file.
func Generation(secret string) string {
	sum := sha256.Sum256([]byte("proxyhub/usage/" + secret))
	return hex.EncodeToString(sum[:8])
}

// Observe records Telemt's current raw octet counter for a user.
func (u *Usage) Observe(name, generation string, raw uint64) {
	if u.Users == nil {
		u.Users = map[string]*UserUsage{}
	}
	e := u.Users[name]
	if e == nil || e.Generation != generation {
		e = &UserUsage{Generation: generation}
		u.Users[name] = e
	}
	if raw >= e.LastRaw {
		e.Total += raw - e.LastRaw
	} else { // Telemt restarted: its counter began again at zero
		e.Total += raw
	}
	e.LastRaw = raw
}

// Total returns the cumulative octets of a user with the given secret generation.
func (u *Usage) Total(name, generation string) uint64 {
	if e := u.Users[name]; e != nil && e.Generation == generation {
		return e.Total
	}
	return 0
}

// Prune forgets users that are no longer desired.
func (u *Usage) Prune(keep map[string]bool) {
	for name := range u.Users {
		if !keep[name] {
			delete(u.Users, name)
		}
	}
}

// Decision explains why a user is not served (for the report's apply_error, never per user).
type Decision struct {
	Expired   int
	OverQuota int
}

// Effective maps desired users to rendered users, disabling the expired and the exhausted.
func Effective(users []desired.User, usage *Usage, now time.Time) ([]telemt.User, Decision) {
	var d Decision
	out := make([]telemt.User, 0, len(users))
	for _, u := range users {
		enabled := u.Enabled
		if u.ExpiresAt != nil && !now.Before(*u.ExpiresAt) {
			if enabled {
				d.Expired++
			}
			enabled = false
		}
		if u.DataQuotaBytes != nil && usage.Total(u.Name, Generation(u.Secret)) >= *u.DataQuotaBytes {
			if enabled {
				d.OverQuota++
			}
			enabled = false
		}
		out = append(out, telemt.User{
			Name:      u.Name,
			Secret:    u.Secret,
			Enabled:   enabled,
			AdTag:     u.AdTag,
			ExpiresAt: u.ExpiresAt,
			DataQuota: u.DataQuotaBytes,
			MaxConns:  u.MaxTCPConns,
		})
	}
	return out, d
}
