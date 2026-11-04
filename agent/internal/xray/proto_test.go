package xray

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestQueryStatsRoundTrip(t *testing.T) {
	// A QueryStatsResponse as Xray encodes it: repeated Stat{name, value}.
	stat := func(name string, v uint64) []byte {
		b := appendString(nil, 1, name)
		return appendUint(b, 2, v)
	}
	var resp []byte
	resp = appendMessage(resp, 1, stat("user>>>cfg-1@proxyhub>>>traffic>>>uplink", 300))
	resp = appendMessage(resp, 1, stat("user>>>cfg-1@proxyhub>>>traffic>>>downlink", 1<<40))
	resp = appendMessage(resp, 1, stat("user>>>cfg-2@proxyhub>>>traffic>>>uplink", 0))
	got, err := decodeQueryStats(resp)
	if err != nil {
		t.Fatal(err)
	}
	if got["user>>>cfg-1@proxyhub>>>traffic>>>downlink"] != 1<<40 || got["user>>>cfg-1@proxyhub>>>traffic>>>uplink"] != 300 {
		t.Fatalf("decoded %v", got)
	}
	if v, ok := got["user>>>cfg-2@proxyhub>>>traffic>>>uplink"]; !ok || v != 0 {
		t.Fatal("a zero value (field left out by proto3) must still decode")
	}
	req := encodeQueryStats("user>>>", false)
	if string(req) != "\x0a\x07user>>>" {
		t.Fatalf("request bytes %q", req)
	}
}

func TestParseFieldsRefusesMalformed(t *testing.T) {
	for name, b := range map[string][]byte{
		"truncated length": {0x0a, 0x05, 'a'},
		"bad varint":       {0x08, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x01},
		"group":            {0x0b},
		"field zero":       {0x00, 0x01},
		"short fixed64":    {0x09, 0x01},
	} {
		if _, err := parseFields(b); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestUserStatNames(t *testing.T) {
	for name, want := range map[string]string{
		"user>>>cfg-7@proxyhub>>>traffic>>>uplink":   "cfg-7@proxyhub",
		"user>>>cfg-7@proxyhub>>>traffic>>>downlink": "cfg-7@proxyhub",
		"user>>>cfg-7@proxyhub>>>online":             "",
		"inbound>>>reality-in>>>traffic>>>uplink":    "",
		"user>>>1.2.3.4>>>traffic>>>uplink":          "",
		"user>>>a>>>b>>>traffic>>>uplink":            "",
	} {
		got, ok := parseUserStat(name)
		if (want == "") == ok || got != want {
			t.Errorf("%q -> %q %v", name, got, ok)
		}
	}
}

func TestAddUserEncoding(t *testing.T) {
	msg := encodeAddUser(InboundTag, User{ID: "6a1f3a2e-1111-4c3b-9a55-0123456789ab", Email: "cfg-1@proxyhub"})
	top, err := parseFields(msg)
	if err != nil || len(top) != 2 || string(top[0].bytes) != InboundTag {
		t.Fatalf("alter inbound %v %v", top, err)
	}
	op, _ := parseFields(top[1].bytes)
	if string(op[0].bytes) != "xray.app.proxyman.command.AddUserOperation" {
		t.Fatalf("operation type %q", op[0].bytes)
	}
	add, _ := parseFields(op[1].bytes)
	user, _ := parseFields(add[0].bytes)
	if string(user[0].bytes) != "cfg-1@proxyhub" || user[0].num != 2 {
		t.Fatalf("user %v", user)
	}
	account, _ := parseFields(user[1].bytes)
	if string(account[0].bytes) != "xray.proxy.vless.Account" {
		t.Fatalf("account type %q", account[0].bytes)
	}
	vless, _ := parseFields(account[1].bytes)
	if string(vless[0].bytes) != "6a1f3a2e-1111-4c3b-9a55-0123456789ab" || string(vless[1].bytes) != Flow || len(vless) != 2 {
		t.Fatalf("vless account %v", vless)
	}
}

// A closed loopback port: the API is down, the error carries nothing from the request.
func TestAPIUnreachable(t *testing.T) {
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := l.Addr().String()
	_ = l.Close()
	api := NewAPI(addr)
	if _, err := api.Uptime(context.Background()); err == nil {
		t.Fatal("expected an error")
	}
	err := api.AddUser(context.Background(), User{ID: "6a1f3a2e-1111-4c3b-9a55-0123456789ab", Email: "cfg-1@proxyhub"})
	if err == nil || err.Error() != "xray api: unreachable" {
		t.Fatalf("error %v", err)
	}
}

func tlsTarget(t *testing.T, cfg func(*tls.Config), h2 bool) (*httptest.Server, *x509.CertPool) {
	t.Helper()
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	srv.Config.ErrorLog = log.New(io.Discard, "", 0) // refused handshakes are the point here
	srv.EnableHTTP2 = h2
	srv.StartTLS()
	if cfg != nil {
		cfg(srv.TLS)
	}
	pool := x509.NewCertPool()
	pool.AddCert(srv.Certificate())
	return srv, pool
}

func TestCheckTarget(t *testing.T) {
	good, pool := tlsTarget(t, nil, true)
	defer good.Close()
	if err := CheckTarget(context.Background(), good.Listener.Addr().String(), "example.com", pool); err != nil {
		t.Fatalf("TLS 1.3 + h2 target refused: %v", err)
	}
	// Wrong name for the certificate.
	if err := CheckTarget(context.Background(), good.Listener.Addr().String(), "other.example.org", pool); err == nil {
		t.Fatal("a certificate for another name was accepted")
	}
	// HTTP/1.1 only (Go's server even refuses the handshake when only h2 is offered).
	noH2, pool2 := tlsTarget(t, nil, false)
	defer noH2.Close()
	if err := CheckTarget(context.Background(), noH2.Listener.Addr().String(), "example.com", pool2); err == nil {
		t.Fatal("HTTP/1.1-only target accepted")
	}
	// TLS 1.3 with a valid certificate but no ALPN at all: the handshake succeeds, and it is the
	// HTTP/2 requirement that refuses it.
	plain, pool4 := tlsTarget(t, func(c *tls.Config) { c.NextProtos = nil }, true)
	defer plain.Close()
	err := CheckTarget(context.Background(), plain.Listener.Addr().String(), "example.com", pool4)
	if err == nil || err.Error() != "target: no HTTP/2" {
		t.Fatalf("a target without HTTP/2 gave %v", err)
	}
	// TLS 1.2 only.
	old, pool3 := tlsTarget(t, func(c *tls.Config) { c.MaxVersion = tls.VersionTLS12 }, true)
	defer old.Close()
	if err := CheckTarget(context.Background(), old.Listener.Addr().String(), "example.com", pool3); err == nil {
		t.Fatal("TLS 1.2 target accepted")
	}
}
