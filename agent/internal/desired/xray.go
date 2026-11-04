package desired

import (
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"regexp"
	"strconv"
	"time"
)

// XrayAPIPort is reserved on the node for Xray's loopback gRPC API (node-image/files/agent-xray.json).
const XrayAPIPort = 10085

// MaxXrayUsers bounds the Reality users of one node.
const MaxXrayUsers = 1000

var (
	// Xray user labels are panel config ids ("cfg-<n>@proxyhub"), never anything about a person.
	// No ">" (Xray's stats name separator), no address shape.
	xrayEmailRE = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}@[a-z][a-z0-9]{0,15}$`)
	uuidRE      = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	shortIDRE   = regexp.MustCompile(`^(?:[0-9a-f]{2}){1,8}$`)
)

// Xray is the node's VLESS + REALITY configuration (docs/10 sections 3.5 and 3.14). The private key
// belongs to the node's key scope (its slot, or the node itself); it is only ever sent to this
// node's authenticated agent and written to a 0600 file.
type Xray struct {
	Port        int        `json:"port"`
	PrivateKey  string     `json:"private_key"`
	Target      string     `json:"target"`
	ServerNames []string   `json:"server_names"`
	ShortIDs    []string   `json:"short_ids"`
	Users       []XrayUser `json:"users"`
}

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

// Validate checks the Xray part. telemtPorts are the ports Telemt listens on (they may not clash).
// Errors never include the private key or a user id.
func (x *Xray) Validate(telemtPorts []int) error {
	if x.Port < 1 || x.Port > 65535 || x.Port == MetricsPort || x.Port == XrayAPIPort {
		return errors.New("desired state: xray port out of range or reserved")
	}
	for _, p := range telemtPorts {
		if p == x.Port || p == XrayAPIPort {
			return errors.New("desired state: a Telemt port clashes with Xray's port or API port")
		}
	}
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
