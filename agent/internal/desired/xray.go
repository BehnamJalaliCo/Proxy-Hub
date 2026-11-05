package desired

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// XrayAPIPort is reserved on the node for Xray's loopback gRPC API (node-image/files/agent-xray.json).
const XrayAPIPort = 10085

// MaxXrayUsers bounds the Reality users of one node.
const MaxXrayUsers = 1000

// Transports of the xray section: "reality" (also when the key is absent, as panels before agent
// 0.3.0 send it) is VLESS + REALITY on TCP; "ws_tls" is VLESS + WebSocket + TLS with the panel's
// certificate for the node's own DNS name.
const (
	TransportReality = "reality"
	TransportWSTLS   = "ws_tls"
)

// Bounds of the PEM material a ws_tls section carries (a chain is a few kilobytes).
const (
	maxCertChain = 32 * 1024
	maxCertKey   = 8 * 1024
)

var (
	// Xray user labels are panel config ids ("cfg-<n>@proxyhub"), never anything about a person.
	// No ">" (Xray's stats name separator), no address shape.
	xrayEmailRE = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}@[a-z][a-z0-9]{0,15}$`)
	uuidRE      = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	shortIDRE   = regexp.MustCompile(`^(?:[0-9a-f]{2}){1,8}$`)
	wsPathRE    = regexp.MustCompile(`^/[A-Za-z0-9_-]{0,64}$`)
)

// Xray is the node's VLESS configuration (docs/10 sections 3.5 and 3.14) for one of two transports.
// REALITY: the private key belongs to the node's key scope (its slot, or the node itself). ws_tls:
// the certificate chain and its key for ServerName (the panel's wildcard certificate). Either key is
// only ever sent to this node's authenticated agent and written to files Xray alone may read.
type Xray struct {
	Transport string `json:"transport,omitempty"`
	Port      int    `json:"port"`
	// REALITY only.
	PrivateKey  string   `json:"private_key,omitempty"`
	Target      string   `json:"target,omitempty"`
	ServerNames []string `json:"server_names,omitempty"`
	ShortIDs    []string `json:"short_ids,omitempty"`
	// ws_tls only: the node's own DNS name, the WebSocket path, the PEM chain (leaf first) and key.
	ServerName   string     `json:"server_name,omitempty"`
	WSPath       string     `json:"ws_path,omitempty"`
	TLSCertChain string     `json:"tls_cert_chain,omitempty"`
	TLSKey       string     `json:"tls_key,omitempty"`
	Users        []XrayUser `json:"users"`
}

// WSTLS reports whether the section asks for VLESS + WebSocket + TLS.
func (x *Xray) WSTLS() bool { return x.Transport == TransportWSTLS }

// XrayUser is one Reality config: a UUID with its own limits. The email is a label for counters.
type XrayUser struct {
	ID           string     `json:"id"`
	Email        string     `json:"email"`
	ExpiresAt    *time.Time `json:"expires_at"`
	DataCapBytes *uint64    `json:"data_cap_bytes"`
}

// ValidXrayEmail reports whether a label may name an Xray user (and its stats counters).
func ValidXrayEmail(email string) bool { return xrayEmailRE.MatchString(email) }

// ValidDNSName reports whether name is a lower-case DNS name (never an IP literal).
func ValidDNSName(name string) bool { return len(name) <= 253 && domainRE.MatchString(name) }

// ValidXrayTarget reports whether target is "dns-name:port", the only shape of a REALITY target.
func ValidXrayTarget(target string) bool {
	host, port, err := net.SplitHostPort(target)
	if err != nil || !ValidDNSName(host) {
		return false
	}
	n, err := strconv.Atoi(port)
	return err == nil && n >= 1 && n <= 65535 && strconv.Itoa(n) == port
}

// ValidateCertificate checks a ws_tls certificate: the chain and key form a pair, and the leaf covers
// serverName. Expiry is left to the panel (the node's clock is not trusted for it). Errors never
// quote the material.
func ValidateCertificate(chainPEM, keyPEM, serverName string) error {
	if len(chainPEM) > maxCertChain || len(keyPEM) > maxCertKey || !strings.Contains(chainPEM, "BEGIN CERTIFICATE") {
		return errors.New("desired state: xray tls certificate chain missing or too large")
	}
	pair, err := tls.X509KeyPair([]byte(chainPEM), []byte(keyPEM))
	if err != nil {
		return errors.New("desired state: xray tls certificate and key do not form a pair")
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return errors.New("desired state: xray tls certificate does not parse")
	}
	if leaf.VerifyHostname(serverName) != nil {
		return errors.New("desired state: xray tls certificate does not cover server_name")
	}
	return nil
}

// Validate checks the Xray part. telemtPorts are the ports Telemt listens on (they may not clash).
// Errors never include a private key, certificate material or a user id.
func (x *Xray) Validate(telemtPorts []int) error {
	if x.Port < 1 || x.Port > 65535 || x.Port == MetricsPort || x.Port == XrayAPIPort {
		return errors.New("desired state: xray port out of range or reserved")
	}
	for _, p := range telemtPorts {
		if p == x.Port || p == XrayAPIPort {
			return errors.New("desired state: a Telemt port clashes with Xray's port or API port")
		}
	}
	switch x.Transport {
	case "", TransportReality:
		if x.ServerName != "" || x.WSPath != "" || x.TLSCertChain != "" || x.TLSKey != "" {
			return errors.New("desired state: ws_tls fields in a reality xray section")
		}
		if err := x.validateReality(); err != nil {
			return err
		}
	case TransportWSTLS:
		if x.PrivateKey != "" || x.Target != "" || len(x.ServerNames) > 0 || len(x.ShortIDs) > 0 {
			return errors.New("desired state: reality fields in a ws_tls xray section")
		}
		if !ValidDNSName(x.ServerName) {
			return errors.New("desired state: xray server_name is not a DNS name")
		}
		if !wsPathRE.MatchString(x.WSPath) {
			return errors.New("desired state: xray ws_path must be / and up to 64 of [A-Za-z0-9_-]")
		}
		if err := ValidateCertificate(x.TLSCertChain, x.TLSKey, x.ServerName); err != nil {
			return err
		}
	default:
		return errors.New("desired state: unknown xray transport")
	}
	return x.validateUsers()
}

func (x *Xray) validateReality() error {
	key, err := base64.RawURLEncoding.DecodeString(x.PrivateKey)
	if err != nil || len(key) != 32 || len(x.PrivateKey) != 43 {
		return errors.New("desired state: xray private_key must be 32 bytes, base64url without padding")
	}
	if !ValidXrayTarget(x.Target) {
		return errors.New("desired state: xray target must be dns-name:port (port 1-65535)")
	}
	if len(x.ServerNames) < 1 || len(x.ServerNames) > 8 {
		return errors.New("desired state: xray needs 1 to 8 server names")
	}
	for _, name := range x.ServerNames {
		if !ValidDNSName(name) {
			return errors.New("desired state: xray server name is not a DNS name")
		}
	}
	if len(x.ShortIDs) < 1 || len(x.ShortIDs) > 8 {
		return errors.New("desired state: xray needs 1 to 8 short ids")
	}
	for _, sid := range x.ShortIDs {
		if !shortIDRE.MatchString(sid) {
			return errors.New("desired state: xray short id must be 2 to 16 hex characters (even)")
		}
	}
	return nil
}

func (x *Xray) validateUsers() error {
	if len(x.Users) > MaxXrayUsers {
		return errors.New("desired state: too many xray users")
	}
	ids, emails := map[string]bool{}, map[string]bool{}
	for i, u := range x.Users {
		if !ValidXrayEmail(u.Email) || emails[u.Email] {
			return fmt.Errorf("desired state: xray user %d: bad or duplicate label", i)
		}
		emails[u.Email] = true
		if !uuidRE.MatchString(u.ID) || ids[u.ID] {
			return fmt.Errorf("desired state: xray user %s: id must be a unique lower-case UUID", u.Email)
		}
		ids[u.ID] = true
		if u.DataCapBytes != nil && *u.DataCapBytes == 0 {
			return fmt.Errorf("desired state: xray user %s: data cap must be positive", u.Email)
		}
	}
	return nil
}
