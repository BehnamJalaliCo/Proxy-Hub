// Package panel is the agent's HTTP client for the panel's /agent/v1 API.
//
// Over HTTPS the panel's server certificate is verified against the system roots (plus an optional
// extra CA file), and the node presents its own client certificate for mTLS. No request or
// response body is ever logged: they carry the registration token, CSRs and Telemt secrets.
package panel

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// MaxResponse bounds any panel response (the desired state of 1000 users is far below this).
const MaxResponse = 4 << 20

// StatusError is a non-success answer from the panel; Code is the panel's short error code.
type StatusError struct {
	Status int
	Code   string
}

func (e *StatusError) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("panel answered %d (%s)", e.Status, e.Code)
	}
	return fmt.Sprintf("panel answered %d", e.Status)
}

// Client talks to the panel.
type Client struct {
	base      string
	http      *http.Client
	userAgent string

	mu   sync.RWMutex
	cert *tls.Certificate
}

// New builds a client. serverCAFile may be empty (system roots only).
func New(base, serverCAFile, agentVersion string) (*Client, error) {
	roots, err := x509.SystemCertPool()
	if err != nil || roots == nil {
		roots = x509.NewCertPool()
	}
	if serverCAFile != "" {
		pemData, err := os.ReadFile(serverCAFile)
		if err != nil {
			return nil, fmt.Errorf("server ca file: %w", err)
		}
		if !roots.AppendCertsFromPEM(pemData) {
			return nil, errors.New("server ca file holds no certificate")
		}
	}
	c := &Client{base: strings.TrimRight(base, "/"), userAgent: "proxyhub-agent/" + agentVersion}
	transport := &http.Transport{
		Proxy: nil, // never route panel traffic through an environment proxy
		TLSClientConfig: &tls.Config{
			MinVersion:           tls.VersionTLS12,
			RootCAs:              roots,
			GetClientCertificate: c.clientCertificate,
		},
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second,
		IdleConnTimeout:       90 * time.Second,
		MaxIdleConns:          2,
	}
	c.http = &http.Client{Transport: transport, Timeout: 60 * time.Second}
	return c, nil
}

// SetCertificate switches the client certificate used from the next TLS handshake on.
func (c *Client) SetCertificate(cert *tls.Certificate) {
	c.mu.Lock()
	c.cert = cert
	c.mu.Unlock()
	c.http.CloseIdleConnections()
}

func (c *Client) clientCertificate(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.cert == nil {
		return &tls.Certificate{}, nil // no certificate yet (registration)
	}
	return c.cert, nil
}

// CertificateResponse is the panel's answer to register and renew.
type CertificateResponse struct {
	CertPEM string `json:"cert_pem"`
	CAPEM   string `json:"ca_pem"`
}

// RegisterRequest is the body of POST /agent/v1/register.
type RegisterRequest struct {
	Token         string  `json:"token"`
	CSRPEM        string  `json:"csr_pem"`
	AgentVersion  string  `json:"agent_version"`
	TelemtVersion *string `json:"telemt_version"`
}

// Register exchanges the one-time token and a CSR for the node's first certificate.
func (c *Client) Register(ctx context.Context, req RegisterRequest) (*CertificateResponse, error) {
	var out CertificateResponse
	if err := c.postJSON(ctx, "/agent/v1/register", req, &out, http.StatusOK); err != nil {
		return nil, err
	}
	return &out, nil
}

// Renew asks for a successor certificate (authenticated with the current one).
func (c *Client) Renew(ctx context.Context, csrPEM string) (*CertificateResponse, error) {
	var out CertificateResponse
	if err := c.postJSON(ctx, "/agent/v1/renew", map[string]string{"csr_pem": csrPEM}, &out, http.StatusOK); err != nil {
		return nil, err
	}
	return &out, nil
}

// Desired fetches the desired state. With a matching etag the panel answers 304 and body is nil.
func (c *Client) Desired(ctx context.Context, etag string) (body []byte, newETag string, err error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/agent/v1/desired", nil)
	if err != nil {
		return nil, "", err
	}
	req.Header.Set("User-Agent", c.userAgent)
	req.Header.Set("Accept", "application/json")
	if etag != "" {
		req.Header.Set("If-None-Match", etag)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, "", fmt.Errorf("desired: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, MaxResponse+1))
	if err != nil {
		return nil, "", fmt.Errorf("desired: %w", err)
	}
	switch resp.StatusCode {
	case http.StatusNotModified:
		return nil, etag, nil
	case http.StatusOK:
		if len(data) > MaxResponse {
			return nil, "", errors.New("desired: response too large")
		}
		return data, resp.Header.Get("ETag"), nil
	default:
		return nil, "", statusError(resp.StatusCode, data)
	}
}

// Report posts a report (204 expected).
func (c *Client) Report(ctx context.Context, report any) error {
	return c.postJSON(ctx, "/agent/v1/report", report, nil, http.StatusNoContent)
}

func (c *Client) postJSON(ctx context.Context, path string, in, out any, want int) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", c.userAgent)
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, MaxResponse+1))
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	if resp.StatusCode != want {
		return statusError(resp.StatusCode, data)
	}
	if out == nil {
		return nil
	}
	if len(data) > MaxResponse {
		return fmt.Errorf("%s: response too large", path)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		return fmt.Errorf("%s: bad response: %w", path, err)
	}
	return nil
}

// statusError keeps only the panel's short error code (never echo a body into logs).
func statusError(status int, body []byte) error {
	var e struct {
		Error  string `json:"error"`
		Detail string `json:"detail"`
	}
	_ = json.Unmarshal(body, &e)
	code := e.Error
	if code == "" {
		code = e.Detail
	}
	if len(code) > 60 || strings.ContainsAny(code, " \n\"") {
		code = ""
	}
	return &StatusError{Status: status, Code: code}
}
