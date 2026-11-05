package xray

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"strconv"
	"time"
)

// CheckTarget verifies from this node what REALITY needs of its target site (docs/10 X5): it
// completes a normal TLS handshake for serverName, with a certificate valid for that name, and the
// site negotiates TLS 1.3 and HTTP/2. roots nil means the system roots. Nothing is sent after the
// handshake. The error names the failed property only.
func CheckTarget(ctx context.Context, target, serverName string, roots *x509.CertPool) error {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	dialer := &tls.Dialer{
		NetDialer: &net.Dialer{Timeout: 10 * time.Second},
		Config: &tls.Config{
			ServerName: serverName,
			RootCAs:    roots,
			MinVersion: tls.VersionTLS13,
			NextProtos: []string{"h2"},
		},
	}
	conn, err := dialer.DialContext(ctx, "tcp", target)
	if err != nil {
		return errors.New("target: no TLS 1.3 handshake with a valid certificate")
	}
	defer conn.Close()
	state := conn.(*tls.Conn).ConnectionState()
	if state.Version != tls.VersionTLS13 {
		return errors.New("target: not TLS 1.3")
	}
	if state.NegotiatedProtocol != "h2" {
		return errors.New("target: no HTTP/2")
	}
	return nil
}

// CheckOwnTLS verifies the node's own ws_tls inbound (docs: the panel only makes a config on a node
// that passed it): a TLS 1.2+ handshake to port on loopback with SNI serverName and ALPN
// http/1.1, whose certificate chain is valid for serverName under roots (nil: the system roots,
// where Let's Encrypt's roots are). Nothing is sent after the handshake.
func CheckOwnTLS(ctx context.Context, port int, serverName string, roots *x509.CertPool) error {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	dialer := &tls.Dialer{
		NetDialer: &net.Dialer{Timeout: 5 * time.Second},
		Config: &tls.Config{
			ServerName: serverName,
			RootCAs:    roots,
			MinVersion: tls.VersionTLS12,
			NextProtos: []string{ALPN},
		},
	}
	conn, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		return errors.New("own tls: no handshake with a certificate valid for the node's name")
	}
	defer conn.Close()
	if conn.(*tls.Conn).ConnectionState().NegotiatedProtocol != ALPN {
		return errors.New("own tls: http/1.1 not negotiated")
	}
	return nil
}
