package xray

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
)

// gRPC methods the agent calls. Nothing else of Xray's API is reachable through this client: in
// particular no method that returns client addresses or online sessions (docs/10 X3, rule 7).
const (
	methodQueryStats   = "/xray.app.stats.command.StatsService/QueryStats"
	methodSysStats     = "/xray.app.stats.command.StatsService/GetSysStats"
	methodAlterInbound = "/xray.app.proxyman.command.HandlerService/AlterInbound"
	maxResponse        = 8 << 20
	userStatPrefix     = "user>>>"
)

// API is a unary gRPC client for Xray's loopback API, over HTTP/2 without TLS (h2c, prior
// knowledge), built on net/http only.
type API struct {
	base string
	http *http.Client
}

// NewAPI returns a client for addr (a loopback "IP:port").
func NewAPI(addr string) *API {
	var protocols http.Protocols
	protocols.SetUnencryptedHTTP2(true)
	transport := &http.Transport{
		Proxy:                 nil, // never through an environment proxy
		Protocols:             &protocols,
		ResponseHeaderTimeout: 10 * time.Second,
		IdleConnTimeout:       60 * time.Second,
		MaxIdleConns:          2,
	}
	return &API{base: "http://" + addr, http: &http.Client{Transport: transport, Timeout: 20 * time.Second}}
}

// StatusError is a non-OK gRPC status. Only the numeric code is kept: Xray's status messages can
// quote request content (a user id), which must never reach a log.
type StatusError struct{ Code int }

func (e *StatusError) Error() string { return fmt.Sprintf("xray api: grpc status %d", e.Code) }

func (c *API) call(ctx context.Context, method string, msg []byte) ([]byte, error) {
	frame := make([]byte, 5, 5+len(msg))
	binary.BigEndian.PutUint32(frame[1:], uint32(len(msg))) //nolint:gosec // our own small messages
	frame = append(frame, msg...)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+method, bytes.NewReader(frame))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/grpc")
	req.Header.Set("TE", "trailers")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, errors.New("xray api: unreachable")
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponse+1))
	if err != nil {
		return nil, errors.New("xray api: response cut off")
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("xray api: http status %d", resp.StatusCode)
	}
	// "Trailers-Only" answers carry grpc-status in the headers; normal ones in the trailers.
	status := resp.Header.Get("Grpc-Status")
	if status == "" {
		status = resp.Trailer.Get("Grpc-Status")
	}
	if status != "0" {
		code, err := strconv.Atoi(status)
		if err != nil {
			code = -1
		}
		return nil, &StatusError{Code: code}
	}
	if len(body) > maxResponse {
		return nil, errors.New("xray api: response too large")
	}
	if len(body) == 0 {
		return nil, nil
	}
	if len(body) < 5 || body[0] != 0 {
		return nil, errors.New("xray api: unexpected frame")
	}
	n := binary.BigEndian.Uint32(body[1:5])
	if uint64(n) != uint64(len(body)-5) {
		return nil, errors.New("xray api: unexpected frame length")
	}
	return body[5:], nil
}

// UserTraffic returns uplink + downlink bytes per user label since Xray started (counters are not
// reset; the agent's tracker turns them into totals across restarts). Only names of the shape
// "user>>><label>>>>traffic>>>uplink|downlink" with a valid label are kept.
func (c *API) UserTraffic(ctx context.Context) (map[string]uint64, error) {
	resp, err := c.call(ctx, methodQueryStats, encodeQueryStats(userStatPrefix, false))
	if err != nil {
		return nil, err
	}
	stats, err := decodeQueryStats(resp)
	if err != nil {
		return nil, err
	}
	out := map[string]uint64{}
	for name, value := range stats {
		email, ok := parseUserStat(name)
		if !ok || value < 0 {
			continue
		}
		out[email] += uint64(value)
	}
	return out, nil
}

func parseUserStat(name string) (string, bool) {
	parts := strings.Split(name, ">>>")
	if len(parts) != 4 || parts[0] != "user" || parts[2] != "traffic" || (parts[3] != "uplink" && parts[3] != "downlink") {
		return "", false
	}
	if !desired.ValidXrayEmail(parts[1]) {
		return "", false
	}
	return parts[1], true
}

// Uptime returns how long the running Xray has been up (seconds).
func (c *API) Uptime(ctx context.Context) (uint32, error) {
	resp, err := c.call(ctx, methodSysStats, nil)
	if err != nil {
		return 0, err
	}
	return decodeSysUptime(resp)
}

// AddUser adds one user to the Reality inbound of the running Xray (no restart).
func (c *API) AddUser(ctx context.Context, u User) error {
	_, err := c.call(ctx, methodAlterInbound, encodeAddUser(InboundTag, u))
	return err
}

// RemoveUser removes one user (by label) from the running Xray: new connections with its id fail.
func (c *API) RemoveUser(ctx context.Context, email string) error {
	_, err := c.call(ctx, methodAlterInbound, encodeRemoveUser(InboundTag, email))
	return err
}
