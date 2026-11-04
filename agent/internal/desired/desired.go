// Package desired decodes and checks the desired state the panel serves at /agent/v1/desired.
//
// The agent trusts the panel, but not blindly: a document that would switch on Telemt's per-IP
// observation (beobachten), turn off the middle proxy the sponsor channel needs, or raise the log
// level is refused outright (CLAUDE.md rule 7, decision D26), as is anything malformed.
package desired

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"
)

// Format is the only document format this agent understands.
const Format = 1

// MetricsPort is reserved on the node for Telemt's loopback metrics listener.
const MetricsPort = 9090

// PlaceholderUser is rendered (disabled, random secret) when no user may be served, because
// Telemt refuses an empty user map and would otherwise fall back to a built-in default user.
const PlaceholderUser = "agent_placeholder"

var (
	hex32RE  = regexp.MustCompile(`^[0-9a-f]{32}$`)
	userRE   = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
	domainRE = regexp.MustCompile(`^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$`)
)

// Document is the whole desired state.
type Document struct {
	Format    int       `json:"format"`
	NodeID    string    `json:"node_id"`
	Intervals Intervals `json:"intervals"`
	Telemt    *Telemt   `json:"telemt"`
	// Xray is present only for nodes that serve VLESS Reality (an older agent refuses the key).
	Xray *Xray `json:"xray,omitempty"`
}

// Intervals tells the agent how often to poll and report.
type Intervals struct {
	PollS   int `json:"poll_s"`
	ReportS int `json:"report_s"`
}

// Telemt is the node's MTProto proxy configuration.
type Telemt struct {
	Ports          []int   `json:"ports"`
	TLSDomain      string  `json:"tls_domain"`
	Modes          Modes   `json:"modes"`
	AdTag          *string `json:"ad_tag"`
	UseMiddleProxy bool    `json:"use_middle_proxy"`
	Beobachten     bool    `json:"beobachten"`
	LogLevel       string  `json:"log_level"`
	Users          []User  `json:"users"`
}

// Modes are Telemt's link modes (classic, secure "dd", Fake-TLS "ee").
type Modes struct {
	Classic bool `json:"classic"`
	Secure  bool `json:"secure"`
	TLS     bool `json:"tls"`
}

// User is one Telemt user: one MTProto secret with its sponsor tag and limits.
type User struct {
	Name           string     `json:"name"`
	Secret         string     `json:"secret"`
	Enabled        bool       `json:"enabled"`
	AdTag          *string    `json:"ad_tag"`
	ExpiresAt      *time.Time `json:"expires_at"`
	DataQuotaBytes *uint64    `json:"data_quota_bytes"`
	MaxTCPConns    *int       `json:"max_tcp_conns"`
}

// Decode parses a desired-state document strictly and validates it for this node.
func Decode(body []byte, nodeID string) (*Document, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var doc Document
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("desired state: %w", err)
	}
	if dec.More() {
		return nil, errors.New("desired state: trailing data")
	}
	if err := doc.Validate(nodeID); err != nil {
		return nil, err
	}
	return &doc, nil
}

// Validate checks the document. It never includes a secret in an error.
func (d *Document) Validate(nodeID string) error {
	if d.Format != Format {
		return fmt.Errorf("desired state: unsupported format %d", d.Format)
	}
	if d.NodeID != nodeID {
		return errors.New("desired state: document is for another node")
	}
	if d.Intervals.PollS < 5 || d.Intervals.PollS > 3600 || d.Intervals.ReportS < 10 || d.Intervals.ReportS > 3600 {
		return errors.New("desired state: intervals out of range")
	}
	var telemtPorts []int
	if d.Telemt != nil {
		if err := d.Telemt.Validate(); err != nil {
			return err
		}
		telemtPorts = d.Telemt.Ports
	}
	if d.Xray != nil {
		return d.Xray.Validate(telemtPorts)
	}
	return nil
}

// Validate checks the Telemt part, including the non-negotiable privacy and sponsor invariants.
func (t *Telemt) Validate() error {
	if !t.UseMiddleProxy {
		return errors.New("desired state refused: use_middle_proxy must be true")
	}
	if t.Beobachten {
		return errors.New("desired state refused: beobachten must be false")
	}
	if t.LogLevel != "silent" {
		return errors.New("desired state refused: log_level must be silent")
	}
	if len(t.Ports) < 1 || len(t.Ports) > 4 {
		return errors.New("desired state: 1 to 4 ports")
	}
	seenPort := map[int]bool{}
	for _, p := range t.Ports {
		if p < 1 || p > 65535 || p == MetricsPort || seenPort[p] {
			return fmt.Errorf("desired state: bad or duplicate port %d", p)
		}
		seenPort[p] = true
	}
	if len(t.TLSDomain) > 253 || !domainRE.MatchString(t.TLSDomain) {
		return errors.New("desired state: tls_domain is not a DNS name")
	}
	if !t.Modes.Classic && !t.Modes.Secure && !t.Modes.TLS {
		return errors.New("desired state: no Telemt mode enabled")
	}
	if t.AdTag != nil && !hex32RE.MatchString(*t.AdTag) {
		return errors.New("desired state: ad_tag must be 32 hex characters")
	}
	if len(t.Users) > 1000 {
		return errors.New("desired state: too many users")
	}
	seen := map[string]bool{}
	for _, u := range t.Users {
		if !ValidUserName(u.Name) || seen[u.Name] {
			return fmt.Errorf("desired state: bad or duplicate user name %q", u.Name)
		}
		seen[u.Name] = true
		if !hex32RE.MatchString(u.Secret) {
			return fmt.Errorf("desired state: user %s: secret must be 32 hex characters", u.Name)
		}
		if u.AdTag != nil && !hex32RE.MatchString(*u.AdTag) {
			return fmt.Errorf("desired state: user %s: ad_tag must be 32 hex characters", u.Name)
		}
		if u.DataQuotaBytes != nil && *u.DataQuotaBytes == 0 {
			return fmt.Errorf("desired state: user %s: data quota must be positive", u.Name)
		}
		if u.MaxTCPConns != nil && *u.MaxTCPConns <= 0 {
			return fmt.Errorf("desired state: user %s: max_tcp_conns must be positive", u.Name)
		}
	}
	return nil
}

// ValidUserName: a TOML bare key and Prometheus label safe name. "include" is refused because
// Telemt expands any line starting with "include =" before parsing; the placeholder is ours.
func ValidUserName(name string) bool {
	return userRE.MatchString(name) && name != "include" && name != PlaceholderUser
}
