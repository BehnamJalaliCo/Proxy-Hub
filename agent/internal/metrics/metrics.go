// Package metrics reads Telemt's Prometheus endpoint and keeps only aggregate counters.
//
// Metric names are Telemt 3.5.16's own (src/metrics/render/process.rs, connections.rs, users.rs).
// Only an allow-list is ever kept: per user (secret) the current connection count and the octet
// counters, and a few process counters (among them two throughput diagnostics: data the middle
// proxy sent that a client's queue had no room for, and connections refused at max_connections). Everything else is dropped while parsing, including the
// per-user unique-IP gauges: the agent never handles anything derived from client addresses, and
// it never requests any other Telemt endpoint (CLAUDE.md rule 7).
package metrics

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Metric names read from Telemt.
const (
	BuildInfo              = "telemt_build_info"
	Uptime                 = "telemt_uptime_seconds"
	ConnectionsTotal       = "telemt_connections_total"
	ConnectionsBadTotal    = "telemt_connections_bad_total"
	HandshakeTimeoutsTotal = "telemt_handshake_timeouts_total"
	UserConnectionsCurrent = "telemt_user_connections_current"
	UserOctetsFromClient   = "telemt_user_octets_from_client_total"
	UserOctetsToClient     = "telemt_user_octets_to_client_total"
	// MeRouteDropsQueueFull: data from Telegram's middle proxy dropped because the client's route
	// queue was full (a client slower than the middle proxy; src/transport/middle_proxy/reader.rs).
	MeRouteDropsQueueFull = "telemt_me_route_drop_queue_full_total"
	// AcceptPermitTimeouts: accepted connections dropped while waiting for a permit
	// (server.max_connections reached).
	AcceptPermitTimeouts = "telemt_accept_permit_timeout_total"
)

var kept = map[string]bool{
	BuildInfo: true, Uptime: true, ConnectionsTotal: true, ConnectionsBadTotal: true,
	HandshakeTimeoutsTotal: true, UserConnectionsCurrent: true, UserOctetsFromClient: true,
	UserOctetsToClient: true, MeRouteDropsQueueFull: true, AcceptPermitTimeouts: true,
}

// MaxBody bounds a scrape (Telemt caps user-labelled series at 4096 users).
const MaxBody = 16 << 20

// Sample is one kept time series value.
type Sample struct {
	Name   string
	Labels map[string]string
	Value  float64
}

// Parse reads the Prometheus text exposition format (version 0.0.4) and returns only the samples
// of the allow-listed metrics. Malformed sample lines are an error.
func Parse(r io.Reader) ([]Sample, error) {
	var out []Sample
	sc := bufio.NewScanner(io.LimitReader(r, MaxBody))
	sc.Buffer(make([]byte, 64*1024), 1<<20)
	line := 0
	for sc.Scan() {
		line++
		text := strings.TrimSpace(sc.Text())
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		name, rest := splitName(text)
		if name == "" {
			return nil, fmt.Errorf("metrics line %d: no metric name", line)
		}
		if !kept[name] {
			continue
		}
		labels := map[string]string{}
		if strings.HasPrefix(rest, "{") {
			var err error
			labels, rest, err = parseLabels(rest)
			if err != nil {
				return nil, fmt.Errorf("metrics line %d: %w", line, err)
			}
		}
		fields := strings.Fields(rest)
		if len(fields) < 1 || len(fields) > 2 {
			return nil, fmt.Errorf("metrics line %d: expected value [timestamp]", line)
		}
		value, err := parseFloat(fields[0])
		if err != nil {
			return nil, fmt.Errorf("metrics line %d: %w", line, err)
		}
		out = append(out, Sample{Name: name, Labels: labels, Value: value})
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("read metrics: %w", err)
	}
	return out, nil
}

func splitName(s string) (string, string) {
	for i, c := range s {
		ok := c == '_' || c == ':' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (i > 0 && c >= '0' && c <= '9')
		if !ok {
			return s[:i], s[i:]
		}
	}
	return s, ""
}

func parseLabels(s string) (map[string]string, string, error) {
	labels := map[string]string{}
	i := 1 // after '{'
	for {
		for i < len(s) && (s[i] == ' ' || s[i] == ',') {
			i++
		}
		if i >= len(s) {
			return nil, "", errors.New("unterminated label set")
		}
		if s[i] == '}' {
			return labels, s[i+1:], nil
		}
		start := i
		for i < len(s) && s[i] != '=' {
			i++
		}
		if i+1 >= len(s) || s[i+1] != '"' {
			return nil, "", errors.New("malformed label")
		}
		name := strings.TrimSpace(s[start:i])
		i += 2
		var v strings.Builder
		for {
			if i >= len(s) {
				return nil, "", errors.New("unterminated label value")
			}
			c := s[i]
			if c == '"' {
				i++
				break
			}
			if c == '\\' && i+1 < len(s) {
				i++
				switch s[i] {
				case 'n':
					v.WriteByte('\n')
				default:
					v.WriteByte(s[i])
				}
			} else {
				v.WriteByte(c)
			}
			i++
		}
		labels[name] = v.String()
	}
}

func parseFloat(s string) (float64, error) {
	switch s {
	case "+Inf", "Inf":
		return math.Inf(1), nil
	case "-Inf":
		return math.Inf(-1), nil
	case "NaN":
		return math.NaN(), nil
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, fmt.Errorf("bad value %q", s)
	}
	return v, nil
}

// UserStats are the counters of one Telemt user (secret).
type UserStats struct {
	CurrentConnections uint64
	OctetsFromClient   uint64
	OctetsToClient     uint64
}

// TotalOctets is the user's traffic in both directions.
func (u UserStats) TotalOctets() uint64 { return u.OctetsFromClient + u.OctetsToClient }

// Stats is what the agent keeps from one scrape.
type Stats struct {
	Version                string
	UptimeS                *float64
	ConnectionsTotal       *uint64
	ConnectionsBadTotal    *uint64
	HandshakeTimeoutsTotal *uint64
	MeRouteDropsTotal      *uint64
	AcceptPermitTimeouts   *uint64
	Users                  map[string]UserStats
}

// Extract turns kept samples into Stats. Series with unexpected labels are ignored.
func Extract(samples []Sample) Stats {
	st := Stats{Users: map[string]UserStats{}}
	counter := func(v float64) *uint64 {
		if v < 0 || math.IsNaN(v) || math.IsInf(v, 0) || v > math.MaxInt64 {
			return nil
		}
		n := uint64(v)
		return &n
	}
	for _, s := range samples {
		switch s.Name {
		case BuildInfo:
			if v, ok := s.Labels["version"]; ok && len(s.Labels) == 1 {
				st.Version = v
			}
		case Uptime:
			if len(s.Labels) == 0 && s.Value >= 0 && !math.IsInf(s.Value, 0) && !math.IsNaN(s.Value) {
				v := s.Value
				st.UptimeS = &v
			}
		case ConnectionsTotal, ConnectionsBadTotal, HandshakeTimeoutsTotal, MeRouteDropsQueueFull, AcceptPermitTimeouts:
			if len(s.Labels) != 0 {
				continue
			}
			c := counter(s.Value)
			switch s.Name {
			case ConnectionsTotal:
				st.ConnectionsTotal = c
			case ConnectionsBadTotal:
				st.ConnectionsBadTotal = c
			case MeRouteDropsQueueFull:
				st.MeRouteDropsTotal = c
			case AcceptPermitTimeouts:
				st.AcceptPermitTimeouts = c
			default:
				st.HandshakeTimeoutsTotal = c
			}
		case UserConnectionsCurrent, UserOctetsFromClient, UserOctetsToClient:
			user, ok := s.Labels["user"]
			c := counter(s.Value)
			if !ok || len(s.Labels) != 1 || c == nil {
				continue
			}
			u := st.Users[user]
			switch s.Name {
			case UserConnectionsCurrent:
				u.CurrentConnections = *c
			case UserOctetsFromClient:
				u.OctetsFromClient = *c
			default:
				u.OctetsToClient = *c
			}
			st.Users[user] = u
		}
	}
	return st
}

// Scrape fetches and parses Telemt's /metrics. url must be the loopback metrics URL from config.
func Scrape(ctx context.Context, client *http.Client, url string) (Stats, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return Stats{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return Stats{}, fmt.Errorf("scrape telemt: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return Stats{}, fmt.Errorf("scrape telemt: status %d", resp.StatusCode)
	}
	samples, err := Parse(resp.Body)
	if err != nil {
		return Stats{}, err
	}
	return Extract(samples), nil
}
