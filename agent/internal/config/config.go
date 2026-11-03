// Package config reads the agent's own settings file, written once by cloud-init
// (/etc/proxyhub-agent/agent.json). Unknown keys are refused so a typo can not silently disable
// a setting. The file holds no secret: the one-time registration token is a separate 0600 file
// that the agent deletes after use.
package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
)

var uuidRE = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// Config is the agent configuration.
type Config struct {
	// PanelURL is the agent endpoint of the panel, e.g. https://agent.example.com. Plain http is
	// accepted only for a loopback host (local tests).
	PanelURL string `json:"panel_url"`
	// NodeID is this node's id in the panel database (lower-case UUID).
	NodeID string `json:"node_id"`
	// CAFile is the panel's agent CA certificate (from cloud-init). The certificate returned at
	// registration must chain to it, and the CA the panel returns must be byte-identical.
	CAFile string `json:"ca_file"`
	// ServerCAFile optionally adds roots for the panel's TLS server certificate (default: the
	// system roots, as the panel uses a public ACME certificate).
	ServerCAFile string `json:"server_ca_file,omitempty"`
	// TokenFile holds the one-time registration token (0600, removed after registration).
	TokenFile string `json:"token_file"`
	// StateDir keeps identity.pem (key + certificate), the last desired state and usage (0700).
	StateDir string `json:"state_dir"`
	// TelemtConfig is the Telemt config file the agent renders (0600, owned by TelemtUser).
	TelemtConfig string `json:"telemt_config"`
	// TelemtDataDir is Telemt's working directory (quota state, caches, TLS front cache).
	TelemtDataDir string `json:"telemt_data_dir"`
	// TelemtUser owns the rendered config so the unprivileged Telemt process can read it.
	// Empty: keep the agent's own uid (tests).
	TelemtUser string `json:"telemt_user,omitempty"`
	// MetricsListen is Telemt's loopback Prometheus listener the agent renders and scrapes.
	MetricsListen string `json:"metrics_listen"`
	// TelemtVersion is the pinned Telemt version installed by the image (versions.lock).
	TelemtVersion string `json:"telemt_version,omitempty"`
	// ReloadCommand and RestartCommand control the Telemt service. Empty: do nothing (tests).
	ReloadCommand  []string `json:"reload_command,omitempty"`
	RestartCommand []string `json:"restart_command,omitempty"`
}

// Load reads and validates the config file.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read agent config: %w", err)
	}
	return Parse(data)
}

// Parse decodes and validates config JSON.
func Parse(data []byte) (*Config, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var c Config
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("agent config: %w", err)
	}
	if dec.More() {
		return nil, errors.New("agent config: trailing data")
	}
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return &c, nil
}

// Validate checks every field.
func (c *Config) Validate() error {
	u, err := url.Parse(c.PanelURL)
	if err != nil || u.Host == "" || u.User != nil || (u.Path != "" && u.Path != "/") || u.RawQuery != "" {
		return errors.New("agent config: panel_url must be scheme://host[:port]")
	}
	switch u.Scheme {
	case "https":
	case "http":
		if !isLoopbackHost(u.Hostname()) {
			return errors.New("agent config: plain http is allowed only for a loopback panel_url")
		}
	default:
		return errors.New("agent config: panel_url scheme must be https")
	}
	if !uuidRE.MatchString(c.NodeID) {
		return errors.New("agent config: node_id must be a lower-case UUID")
	}
	for name, p := range map[string]string{
		"ca_file": c.CAFile, "token_file": c.TokenFile, "state_dir": c.StateDir,
		"telemt_config": c.TelemtConfig, "telemt_data_dir": c.TelemtDataDir,
	} {
		if !filepath.IsAbs(p) {
			return fmt.Errorf("agent config: %s must be an absolute path", name)
		}
	}
	if c.ServerCAFile != "" && !filepath.IsAbs(c.ServerCAFile) {
		return errors.New("agent config: server_ca_file must be an absolute path")
	}
	host, port, err := net.SplitHostPort(c.MetricsListen)
	if err != nil || !isLoopbackHost(host) || host == "localhost" {
		return errors.New("agent config: metrics_listen must be a loopback IP:port")
	}
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
		return errors.New("agent config: metrics_listen port out of range")
	}
	return nil
}

func isLoopbackHost(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// MetricsURL is the only Telemt HTTP endpoint the agent ever requests (aggregate counters).
func (c *Config) MetricsURL() string {
	return "http://" + c.MetricsListen + "/metrics"
}

// IdentityFile holds the node's private key and current certificate (PEM, 0600).
func (c *Config) IdentityFile() string { return filepath.Join(c.StateDir, "identity.pem") }

// DesiredFile caches the last desired state so the node keeps working while the panel is away.
func (c *Config) DesiredFile() string { return filepath.Join(c.StateDir, "desired.json") }

// UsageFile keeps per-user byte usage across Telemt restarts for local quota enforcement.
func (c *Config) UsageFile() string { return filepath.Join(c.StateDir, "usage.json") }
