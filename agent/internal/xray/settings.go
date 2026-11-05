// Package xray drives Xray-core on a node from the panel's desired state (docs/10 sections 3.5 and
// 3.14, decision D18): VLESS + REALITY on TCP, or (transport ws_tls, 2026-11-05) VLESS + WebSocket +
// TLS with the panel's certificate for the node's own DNS name. A REALITY section may add (agent
// 0.4.0) a second inbound on its own port: VLESS + XHTTP + REALITY with the same key and users:
//
//   - Render turns the desired "xray" section into Xray's JSON config. The privacy settings are
//     constants here, not data: access log "none", warning level with every address masked, no DNS
//     log, statsUserOnline false, the gRPC API on loopback only (CLAUDE.md rule 7). CheckInvariants
//     refuses any config bytes that break them, before anything is written.
//   - Plan compares the running config with the next one. When only users changed, they are added
//     and removed live through Xray's HandlerService.AlterInbound (no restart, docs/10 X2);
//     anything else is a validated (xray run -test) file swap and a restart.
//   - The API client reads per-user traffic counters with StatsService.QueryStats and the process
//     uptime with GetSysStats. It has no method that could return a client address (docs/10 X3).
//   - CheckTarget verifies from the node that the REALITY target speaks TLS 1.3 with HTTP/2;
//     CheckOwnTLS that the node's own ws_tls inbound serves a valid certificate for its name.
//
// The agent never logs a private key, a user id or a config, and Xray's own output goes nowhere.
package xray

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
)

// SettingsFile is where provision.sh installs node-image/files/agent-xray.json.
const SettingsFile = "/etc/proxyhub-agent/xray.json"

// Settings are the node's Xray paths and commands (no secret).
type Settings struct {
	// Config is the Xray config file the agent renders (0600, owned by User).
	Config string `json:"config"`
	// Binary is the pinned Xray binary (versions.lock), used for "xray run -test".
	Binary string `json:"binary"`
	// AssetsDir holds geoip.dat (XRAY_LOCATION_ASSET).
	AssetsDir string `json:"assets_dir"`
	// API is Xray's loopback gRPC listener (rendered into the config and used by the agent).
	API string `json:"api"`
	// User owns the rendered config so the unprivileged Xray process can read it. Empty: keep the
	// agent's own uid (tests).
	User string `json:"user,omitempty"`
	// Version is the pinned Xray version (versions.lock), reported to the panel.
	Version string `json:"version,omitempty"`
	// TLSDir holds the ws_tls certificate and key files (0640, readable by User). Empty: "tls" next
	// to Config (/etc/xray/tls: Xray's own read-only directory, already open to the xray group).
	TLSDir string `json:"tls_dir,omitempty"`
	// RestartCommand (re)starts xray.service; StopCommand stops it. Empty: do nothing (tests).
	RestartCommand []string `json:"restart_command,omitempty"`
	StopCommand    []string `json:"stop_command,omitempty"`
}

// LoadSettings reads the settings file. A node without it (an older image) serves no Xray.
func LoadSettings(path string) (*Settings, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var s Settings
	if err := dec.Decode(&s); err != nil {
		return nil, fmt.Errorf("xray settings: %w", err)
	}
	if dec.More() {
		return nil, errors.New("xray settings: trailing data")
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	return &s, nil
}

// Validate checks every field.
func (s *Settings) Validate() error {
	for name, p := range map[string]string{"config": s.Config, "binary": s.Binary, "assets_dir": s.AssetsDir} {
		if !filepath.IsAbs(p) {
			return fmt.Errorf("xray settings: %s must be an absolute path", name)
		}
	}
	if s.TLSDir != "" && !filepath.IsAbs(s.TLSDir) {
		return errors.New("xray settings: tls_dir must be an absolute path")
	}
	host, port, err := net.SplitHostPort(s.API)
	ip := net.ParseIP(host)
	if err != nil || ip == nil || !ip.IsLoopback() {
		return errors.New("xray settings: api must be a loopback IP:port")
	}
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
		return errors.New("xray settings: api port out of range")
	}
	return nil
}

// CertDir is where the ws_tls certificate files go.
func (s *Settings) CertDir() string {
	if s.TLSDir != "" {
		return s.TLSDir
	}
	return filepath.Join(filepath.Dir(s.Config), "tls")
}
