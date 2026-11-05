package xray

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/enforce"
)

// Fixed parts of every rendered config.
const (
	// InboundTag is the REALITY inbound, WSInboundTag the VLESS + WebSocket + TLS one (one of the two
	// is rendered, on the same port).
	InboundTag   = "reality-in"
	WSInboundTag = "vless-ws-in"
	// Flow is XTLS Vision, the flow every REALITY link the panel publishes asks for. A WebSocket
	// link has no flow (Vision needs the raw TLS stream).
	Flow = "xtls-rprx-vision"
	// ALPN and TLSMinVersion of the ws_tls inbound: HTTP/1.1 (WebSocket upgrades), TLS 1.2 or 1.3.
	ALPN          = "http/1.1"
	TLSMinVersion = "1.2"
	userLevel     = 0
	blockTag      = "block"
	directTag     = "direct"
	apiTag        = "api"
	maskFull      = "full"
	accessNone    = "none"
	// LogLevel: warnings only, and with maskAddress "full" no address appears even in those.
	// Xray's output is discarded by xray.service as well (rule 7).
	LogLevel = "warning"
)

// blockedIPs never leave the node through Xray: private and special ranges (geoip:private, from the
// pinned geoip.dat), loopback (the node's own API ports) and the metadata service. The node
// firewall drops the same egress again (node-image/files/nftables.conf) except loopback.
var blockedIPs = []string{"geoip:private", "127.0.0.0/8", "::1/128", "169.254.169.254/32"}

// mailPorts are never dialed (abuse), like the firewall's egress rule.
const mailPorts = "25,465,587"

// inboundListen: the Reality inbound answers on every IPv4 address of the node (port 443 is opened
// by the node firewall); anything narrower would make every link fail.
const inboundListen = "0.0.0.0"

// User is one Reality user as rendered: id and stats label.
type User struct {
	ID    string
	Email string
}

// Decision counts users the agent itself took out (expired or over their data cap).
type Decision struct {
	Expired   int
	OverQuota int
}

// Effective returns the desired users that may still be served, in a stable order. Expiry and data
// cap hold on the node even while the panel is unreachable (docs/09 R10); usage is the agent's own
// tracker of Xray's counters (keyed by email, generation derived from the user id).
func Effective(users []desired.XrayUser, usage *enforce.Usage, now time.Time) ([]User, Decision) {
	var d Decision
	out := make([]User, 0, len(users))
	for _, u := range users {
		if u.ExpiresAt != nil && !now.Before(*u.ExpiresAt) {
			d.Expired++
			continue
		}
		if u.DataCapBytes != nil && usage.Total(u.Email, enforce.Generation(u.ID)) >= *u.DataCapBytes {
			d.OverQuota++
			continue
		}
		out = append(out, User{ID: u.ID, Email: u.Email})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Email < out[j].Email })
	return out, d
}

// --- the config document (only the parts we use; field names are Xray's) ---

type config struct {
	Log       logConfig    `json:"log"`
	API       apiConfig    `json:"api"`
	Stats     struct{}     `json:"stats"`
	Policy    policyConfig `json:"policy"`
	Inbounds  []inbound    `json:"inbounds"`
	Outbounds []outbound   `json:"outbounds"`
	Routing   routing      `json:"routing"`
}

type logConfig struct {
	Access      string `json:"access"`
	Error       string `json:"error"`
	Level       string `json:"loglevel"`
	DNSLog      bool   `json:"dnsLog"`
	MaskAddress string `json:"maskAddress"`
}

type apiConfig struct {
	Tag      string   `json:"tag"`
	Listen   string   `json:"listen"`
	Services []string `json:"services"`
}

type policyConfig struct {
	Levels map[string]levelPolicy `json:"levels"`
	System systemPolicy           `json:"system"`
}

type levelPolicy struct {
	StatsUserUplink   bool `json:"statsUserUplink"`
	StatsUserDownlink bool `json:"statsUserDownlink"`
	// Must stay false: true makes Xray track every user's client addresses (docs/10 X3).
	StatsUserOnline bool `json:"statsUserOnline"`
}

type systemPolicy struct {
	StatsInboundUplink    bool `json:"statsInboundUplink"`
	StatsInboundDownlink  bool `json:"statsInboundDownlink"`
	StatsOutboundUplink   bool `json:"statsOutboundUplink"`
	StatsOutboundDownlink bool `json:"statsOutboundDownlink"`
}

type inbound struct {
	Tag            string          `json:"tag"`
	Listen         string          `json:"listen"`
	Port           int             `json:"port"`
	Protocol       string          `json:"protocol"`
	Settings       inboundSettings `json:"settings"`
	StreamSettings inboundStream   `json:"streamSettings"`
	Sniffing       sniffing        `json:"sniffing"`
}

type inboundSettings struct {
	Clients    []client `json:"clients"`
	Decryption string   `json:"decryption"`
}

type client struct {
	ID    string `json:"id"`
	Email string `json:"email"`
	Flow  string `json:"flow"`
	Level int    `json:"level"`
}

type inboundStream struct {
	Network  string           `json:"network"`
	Security string           `json:"security"`
	Reality  *realitySettings `json:"realitySettings,omitempty"`
	TLS      *tlsSettings     `json:"tlsSettings,omitempty"`
	WS       *wsSettings      `json:"wsSettings,omitempty"`
}

// tlsSettings: only certificate FILES, never inline key material in the config.
type tlsSettings struct {
	ALPN         []string      `json:"alpn"`
	MinVersion   string        `json:"minVersion"`
	Certificates []certificate `json:"certificates"`
}

type certificate struct {
	CertificateFile string `json:"certificateFile"`
	KeyFile         string `json:"keyFile"`
}

type wsSettings struct {
	Path string `json:"path"`
}

// TLSFiles are the certificate chain and key files the ws_tls inbound reads (written by the agent).
type TLSFiles struct {
	Cert string
	Key  string
}

type realitySettings struct {
	Show        bool     `json:"show"`
	Target      string   `json:"target"`
	Xver        int      `json:"xver"`
	ServerNames []string `json:"serverNames"`
	PrivateKey  string   `json:"privateKey"`
	ShortIDs    []string `json:"shortIds"`
}

type sniffing struct {
	Enabled      bool     `json:"enabled"`
	DestOverride []string `json:"destOverride"`
	RouteOnly    bool     `json:"routeOnly"`
}

type outbound struct {
	Tag            string           `json:"tag"`
	Protocol       string           `json:"protocol"`
	Settings       *freedomSettings `json:"settings,omitempty"`
	StreamSettings *outboundStream  `json:"streamSettings,omitempty"`
}

type freedomSettings struct {
	FinalRules []finalRule `json:"finalRules"`
}

type finalRule struct {
	Action  string   `json:"action"`
	Network string   `json:"network,omitempty"`
	Port    string   `json:"port,omitempty"`
	IP      []string `json:"ip,omitempty"`
}

type outboundStream struct {
	Sockopt sockopt `json:"sockopt"`
}

type sockopt struct {
	DomainStrategy string `json:"domainStrategy"`
}

type routing struct {
	DomainStrategy string `json:"domainStrategy"`
	Rules          []rule `json:"rules"`
}

type rule struct {
	Type        string   `json:"type"`
	Protocol    []string `json:"protocol,omitempty"`
	IP          []string `json:"ip,omitempty"`
	Port        string   `json:"port,omitempty"`
	OutboundTag string   `json:"outboundTag"`
}

// InboundOf is the inbound tag and client flow the section renders.
func InboundOf(x *desired.Xray) (tag, flow string) {
	if x.WSTLS() {
		return WSInboundTag, ""
	}
	return InboundTag, Flow
}

func renderStream(x *desired.Xray, files TLSFiles) (inboundStream, error) {
	if !x.WSTLS() {
		return inboundStream{
			Network:  "tcp",
			Security: "reality",
			Reality: &realitySettings{
				Show:        false,
				Target:      x.Target,
				Xver:        0,
				ServerNames: x.ServerNames,
				PrivateKey:  x.PrivateKey,
				ShortIDs:    x.ShortIDs,
			},
		}, nil
	}
	if !filepath.IsAbs(files.Cert) || !filepath.IsAbs(files.Key) {
		return inboundStream{}, errors.New("xray: the ws_tls inbound needs absolute certificate and key files")
	}
	return inboundStream{
		Network:  "ws",
		Security: "tls",
		TLS: &tlsSettings{
			ALPN:         []string{ALPN},
			MinVersion:   TLSMinVersion,
			Certificates: []certificate{{CertificateFile: files.Cert, KeyFile: files.Key}},
		},
		WS: &wsSettings{Path: x.WSPath},
	}, nil
}

// Render builds the Xray config for the desired section and the effective users: a REALITY inbound,
// or (ws_tls) a VLESS + WebSocket + TLS inbound that reads files (written by the agent). The result
// passes CheckInvariants (it is checked here before it is returned).
func Render(x *desired.Xray, users []User, apiListen string, files TLSFiles) ([]byte, error) {
	if x == nil {
		return nil, errors.New("xray: nothing to render")
	}
	tag, flow := InboundOf(x)
	stream, err := renderStream(x, files)
	if err != nil {
		return nil, err
	}
	clients := make([]client, 0, len(users))
	for _, u := range users {
		clients = append(clients, client{ID: u.ID, Email: u.Email, Flow: flow, Level: userLevel})
	}
	cfg := config{
		Log: logConfig{Access: accessNone, Error: "", Level: LogLevel, DNSLog: false, MaskAddress: maskFull},
		API: apiConfig{Tag: apiTag, Listen: apiListen, Services: []string{"HandlerService", "StatsService"}},
		Policy: policyConfig{
			Levels: map[string]levelPolicy{"0": {StatsUserUplink: true, StatsUserDownlink: true, StatsUserOnline: false}},
		},
		Inbounds: []inbound{{
			Tag:            tag,
			Listen:         inboundListen,
			Port:           x.Port,
			Protocol:       "vless",
			Settings:       inboundSettings{Clients: clients, Decryption: "none"},
			StreamSettings: stream,
			// Sniffing only to route (bittorrent block); destinations are never rewritten.
			Sniffing: sniffing{Enabled: true, DestOverride: []string{"http", "tls", "quic"}, RouteOnly: true},
		}},
		Outbounds: []outbound{
			{
				Tag:      directTag,
				Protocol: "freedom",
				// Checked on the address actually dialed, after DNS: no domain can lead to loopback or
				// a private range (DNS rebinding included).
				Settings: &freedomSettings{FinalRules: []finalRule{
					{Action: "block", IP: blockedIPs},
					{Action: "block", Network: "tcp", Port: mailPorts},
				}},
				StreamSettings: &outboundStream{Sockopt: sockopt{DomainStrategy: "UseIP"}},
			},
			{Tag: blockTag, Protocol: "blackhole"},
		},
		Routing: routing{
			DomainStrategy: "AsIs",
			Rules: []rule{
				{Type: "field", Protocol: []string{"bittorrent"}, OutboundTag: blockTag},
				{Type: "field", IP: blockedIPs, OutboundTag: blockTag},
				{Type: "field", Port: mailPorts, OutboundTag: blockTag},
			},
		},
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return nil, err
	}
	data = append(data, '\n')
	if err := CheckInvariants(data); err != nil {
		return nil, err
	}
	return data, nil
}

// ErrRefused marks a config that breaks a privacy or safety invariant.
var ErrRefused = errors.New("xray config refused")

func refuse(why string) error { return fmt.Errorf("%w: %s", ErrRefused, why) }

// CheckInvariants re-reads config bytes (whatever produced them) and refuses anything that could
// log or expose a user's address, open the API beyond loopback, or let traffic reach the node's own
// services, private ranges or the metadata service. Messages never quote config content.
func CheckInvariants(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var c config
	if err := dec.Decode(&c); err != nil {
		return refuse("not a config this agent renders")
	}
	if dec.More() {
		return refuse("trailing data")
	}
	if c.Log.Access != accessNone || c.Log.DNSLog || c.Log.MaskAddress != maskFull || c.Log.Error != "" {
		return refuse("access log, DNS log and unmasked addresses must stay off")
	}
	switch c.Log.Level {
	case "none", "error", "warning":
	default:
		return refuse("log level above warning")
	}
	if c.API.Tag != apiTag || !loopbackAddr(c.API.Listen) {
		return refuse("API must listen on loopback")
	}
	for _, s := range c.API.Services {
		if s != "HandlerService" && s != "StatsService" {
			return refuse("API service not allowed")
		}
	}
	for _, lv := range c.Policy.Levels {
		if lv.StatsUserOnline {
			return refuse("per-user online tracking must stay off")
		}
	}
	if c.Policy.System != (systemPolicy{}) {
		return refuse("only per-user counters are kept")
	}
	if len(c.Inbounds) != 1 {
		return refuse("exactly one inbound")
	}
	in := c.Inbounds[0]
	if in.Listen != inboundListen || in.Protocol != "vless" || in.Settings.Decryption != "none" || !in.Sniffing.RouteOnly {
		return refuse("the inbound must be VLESS on every address, without decryption")
	}
	flow, err := checkStream(in.Tag, in.StreamSettings)
	if err != nil {
		return err
	}
	for _, cl := range in.Settings.Clients {
		if !desired.ValidXrayEmail(cl.Email) || cl.Flow != flow || cl.Level != userLevel {
			return refuse("bad client entry")
		}
	}
	if len(c.Outbounds) != 2 || c.Outbounds[0].Tag != directTag || c.Outbounds[0].Protocol != "freedom" ||
		c.Outbounds[1].Tag != blockTag || c.Outbounds[1].Protocol != "blackhole" || c.Outbounds[1].Settings != nil {
		return refuse("outbounds must be freedom and blackhole")
	}
	free := c.Outbounds[0].Settings
	if free == nil || len(free.FinalRules) < 2 || free.FinalRules[0].Action != "block" ||
		!sameStrings(free.FinalRules[0].IP, blockedIPs) || !mailBlock(free.FinalRules[1]) {
		return refuse("freedom must block private ranges, loopback, metadata and mail ports after DNS")
	}
	rules := c.Routing.Rules
	if len(rules) < 3 || rules[0].OutboundTag != blockTag || !sameStrings(rules[0].Protocol, []string{"bittorrent"}) ||
		rules[1].OutboundTag != blockTag || !sameStrings(rules[1].IP, blockedIPs) ||
		rules[2].OutboundTag != blockTag || rules[2].Port != mailPorts {
		return refuse("routing must block bittorrent, private ranges and mail ports first")
	}
	return nil
}

// checkStream accepts exactly the two inbounds Render makes and returns their client flow.
func checkStream(tag string, st inboundStream) (string, error) {
	switch tag {
	case InboundTag:
		r := st.Reality
		if st.Security != "reality" || st.Network != "tcp" || r == nil || st.TLS != nil || st.WS != nil || r.Show || r.Xver != 0 {
			return "", refuse("the REALITY inbound must be VLESS REALITY on TCP")
		}
		return Flow, nil
	case WSInboundTag:
		t := st.TLS
		if st.Security != "tls" || st.Network != "ws" || t == nil || st.WS == nil || st.Reality != nil ||
			!sameStrings(t.ALPN, []string{ALPN}) || t.MinVersion != TLSMinVersion || len(t.Certificates) != 1 ||
			!filepath.IsAbs(t.Certificates[0].CertificateFile) || !filepath.IsAbs(t.Certificates[0].KeyFile) ||
			!strings.HasPrefix(st.WS.Path, "/") {
			return "", refuse("the ws_tls inbound must be VLESS + WebSocket + TLS 1.2+ with certificate files")
		}
		return "", nil
	default:
		return "", refuse("unknown inbound")
	}
}

func mailBlock(r finalRule) bool {
	return r.Action == "block" && r.Network == "tcp" && r.Port == mailPorts && len(r.IP) == 0
}

func loopbackAddr(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
