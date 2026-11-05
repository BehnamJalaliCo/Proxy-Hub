package xray

// VLESS + XHTTP + REALITY against the real, pinned Xray binary on both ends (decision D10), next to
// the REALITY and WebSocket tests in real_test.go: the server is the config Render makes for a
// REALITY section with an xhttp part (two inbounds, one key, the same users), the client has the
// exact shape of the published XHTTP link (security=reality, sni, fp=chrome, pbk, sid, type=xhttp,
// path, mode=auto). Skipped without PROXYHUB_TEST_XRAY, like the other real tests.

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
)

const xhttpTestPath = "/0123456789abcdef"

// newXHTTPLab is the REALITY lab with an xhttp part on a port of its own. (The lab's target is a
// loopback address, which only the desired-state check refuses; desired's tests cover it.)
func newXHTTPLab(t *testing.T) *realityLab {
	t.Helper()
	lab := newRealityLab(t)
	lab.x.XHTTP = &desired.XHTTP{Port: freePort(t), Path: xhttpTestPath}
	return lab
}

// server runs cfg and waits for its API.
func (lab *realityLab) server(t *testing.T, cfg []byte) (*API, *lockedBuffer) {
	t.Helper()
	out := startXray(t, lab.bin, lab.assets, cfg)
	api := NewAPI(fmt.Sprintf("127.0.0.1:%d", lab.apiPort))
	waitFor(t, "the server's API", func() bool { _, err := api.Uptime(context.Background()); return err == nil })
	return api, out
}

// client starts a real Xray client for one user whose two local ports lead through the server's
// port (with the given flow and stream settings) to the private service on loopback and on the
// LAN address.
func (lab *realityLab) client(t *testing.T, user User, port int, flow string, stream map[string]any) (loopPort, lanPort int) {
	t.Helper()
	loopPort, lanPort = freePort(t), freePort(t)
	forward := func(local int, address string) map[string]any {
		return map[string]any{
			"listen": "127.0.0.1", "port": local, "protocol": "dokodemo-door",
			"settings": map[string]any{"address": address, "port": lab.privatePort, "network": "tcp"},
		}
	}
	account := map[string]any{"id": user.ID, "encryption": "none"}
	if flow != "" {
		account["flow"] = flow
	}
	clientCfg := map[string]any{
		"log":      map[string]any{"loglevel": "warning", "access": "none"},
		"inbounds": []any{forward(loopPort, "127.0.0.1"), forward(lanPort, lab.lan)},
		"outbounds": []any{map[string]any{
			"protocol": "vless",
			"settings": map[string]any{"vnext": []any{map[string]any{
				"address": "127.0.0.1", "port": port, "users": []any{account},
			}}},
			"streamSettings": stream,
		}},
	}
	clientJSON, _ := json.Marshal(clientCfg)
	startXray(t, lab.bin, lab.assets, clientJSON)
	waitFor(t, "the client's listener", func() bool {
		c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", lanPort), time.Second)
		if err == nil {
			_ = c.Close()
		}
		return err == nil
	})
	return loopPort, lanPort
}

// realityStream is the client side of the REALITY handshake; pub is the public key it pins.
func (lab *realityLab) realityStream(pub string) map[string]any {
	return map[string]any{
		"serverName": "example.com", "fingerprint": "chrome", "password": pub, "shortId": "0123456789abcdef",
	}
}

// xhttpClient is a client with the XHTTP link's shape: type=xhttp, path, mode=auto, REALITY.
func (lab *realityLab) xhttpClient(t *testing.T, user User, path, pub string) (loopPort, lanPort int) {
	t.Helper()
	return lab.client(t, user, lab.x.XHTTP.Port, "", map[string]any{
		"network": "xhttp", "security": "reality",
		"realitySettings": lab.realityStream(pub),
		"xhttpSettings":   map[string]any{"path": path, "mode": "auto"},
	})
}

// tcpClient is a client with the REALITY (TCP) link's shape: flow xtls-rprx-vision on raw TCP.
func (lab *realityLab) tcpClient(t *testing.T, user User) (loopPort, lanPort int) {
	t.Helper()
	return lab.client(t, user, lab.x.Port, Flow, map[string]any{
		"network": "tcp", "security": "reality", "realitySettings": lab.realityStream(lab.pub),
	})
}

// openConfig is cfg with the private-range blocks taken out and only the LAN address allowed.
func (lab *realityLab) openConfig(t *testing.T, cfg []byte) []byte {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(cfg, &m); err != nil {
		t.Fatal(err)
	}
	m["routing"].(map[string]any)["rules"] = []any{}
	m["outbounds"].([]any)[0].(map[string]any)["settings"] = map[string]any{
		"finalRules": []any{map[string]any{"action": "allow", "ip": []any{lab.lan + "/32"}}},
	}
	open, _ := json.Marshal(m)
	return open
}

// The rendered two-inbound config is accepted by the pinned binary, and the binary refuses the
// XHTTP inbound with a transport it can not secure with REALITY (the config test is real).
func TestRealXrayAcceptsTheXHTTPConfig(t *testing.T) {
	lab := newXHTTPLab(t)
	path := t.TempDir() + "/config.json"
	if err := WriteConfig(path, lab.render(t), fsutil.KeepOwner); err != nil {
		t.Fatal(err)
	}
	svc := Service{Binary: lab.bin, AssetsDir: lab.assets}
	if err := svc.Test(context.Background(), path); err != nil {
		t.Fatalf("the pinned Xray refuses the rendered xhttp config: %v", err)
	}
	var m map[string]any
	_ = json.Unmarshal(lab.render(t), &m)
	stream := m["inbounds"].([]any)[1].(map[string]any)["streamSettings"].(map[string]any)
	stream["network"] = "ws" // REALITY only carries RAW, XHTTP and gRPC
	broken, _ := json.Marshal(m)
	if err := os.WriteFile(path, broken, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := svc.Test(context.Background(), path); err == nil {
		t.Fatal("xray run -test accepted REALITY over WebSocket")
	}
}

// Positive control: with the private-range blocks taken out and only the LAN address allowed, a
// request travels through a real VLESS + XHTTP + REALITY connection made from the published link's
// shape, and the user's counter (the same one the TCP inbound feeds: one data cap per config)
// covers request and response. The TCP (Vision) link of the same user works at the same time. A
// client on another path, or one that pins another REALITY public key, is not served.
func TestRealXrayXHTTPCarriesTrafficWhenAllowed(t *testing.T) {
	lab := newXHTTPLab(t)
	api, _ := lab.server(t, lab.openConfig(t, lab.render(t)))
	_, lanPort := lab.xhttpClient(t, lab.user, xhttpTestPath, lab.pub)
	if got := request(t, lanPort); !strings.HasPrefix(got, "HTTP/1.1 200") {
		t.Fatalf("no answer through VLESS + XHTTP + REALITY: %q", got)
	}
	if lab.hits.Load() != 1 {
		t.Fatalf("hits %d", lab.hits.Load())
	}
	var viaXHTTP uint64
	waitFor(t, "request and response bytes", func() bool {
		viaXHTTP = userTraffic(t, api, lab.user.Email)
		return viaXHTTP > 150
	})

	_, tcpLan := lab.tcpClient(t, lab.user)
	if got := request(t, tcpLan); !strings.HasPrefix(got, "HTTP/1.1 200") {
		t.Fatalf("the TCP link stopped working next to XHTTP: %q", got)
	}
	waitFor(t, "the TCP link's bytes on the same counter", func() bool { return userTraffic(t, api, lab.user.Email) > viaXHTTP })
	if lab.hits.Load() != 2 {
		t.Fatalf("hits %d", lab.hits.Load())
	}

	_, wrongPath := lab.xhttpClient(t, lab.user, "/not-the-path", lab.pub)
	if got := request(t, wrongPath); got != "" {
		t.Fatalf("a client on another path was served: %q", got)
	}
	_, otherPub := x25519Pair(t)
	_, wrongKey := lab.xhttpClient(t, lab.user, xhttpTestPath, otherPub)
	if got := request(t, wrongKey); got != "" {
		t.Fatalf("a client with another REALITY key was served: %q", got)
	}
	stranger := User{ID: "0e5c1d2a-3333-4c3b-9a55-0123456789ab", Email: "cfg-9@proxyhub"}
	_, unknown := lab.xhttpClient(t, stranger, xhttpTestPath, lab.pub)
	if got := request(t, unknown); got != "" {
		t.Fatalf("an unknown user was served: %q", got)
	}
	if lab.hits.Load() != 2 {
		t.Fatalf("a wrong path, key or user reached the service: %d hits", lab.hits.Load())
	}
}

// The rendered config as it is: through XHTTP the user is counted and private destinations
// (loopback, LAN) stay blocked. The agent's own live path (Decide, then the API calls of its plan
// in every inbound) takes the user out of both inbounds and puts it back without a restart.
// Xray's own output shows no address (rule 7).
func TestRealXrayXHTTPUsersCountersAndBlocking(t *testing.T) {
	lab := newXHTTPLab(t)
	withUser := lab.render(t)
	api, serverLog := lab.server(t, withUser)
	loopPort, lanPort := lab.xhttpClient(t, lab.user, xhttpTestPath, lab.pub)
	_, tcpLan := lab.tcpClient(t, lab.user)
	ctx := context.Background()
	email := lab.user.Email

	if got := request(t, loopPort); got != "" {
		t.Fatalf("a loopback destination answered through XHTTP: %q", got)
	}
	if got := request(t, lanPort); got != "" {
		t.Fatalf("a private destination answered through XHTTP: %q", got)
	}
	waitFor(t, "the user's traffic counter", func() bool { return userTraffic(t, api, email) > 0 })

	// The agent's live plan for "no users": one plan, both inbounds, the right flows.
	empty, err := Render(lab.x, nil, fmt.Sprintf("127.0.0.1:%d", lab.apiPort), TLSFiles{})
	if err != nil {
		t.Fatal(err)
	}
	plan, err := Decide(withUser, empty)
	if err != nil || plan.Kind != KindLive || len(plan.Remove) != 1 ||
		fmt.Sprint(plan.Inbounds) != fmt.Sprint([]LiveInbound{{InboundTag, Flow}, {XHTTPInboundTag, ""}}) {
		t.Fatalf("live removal plan %+v %v", plan, err)
	}
	applyPlan := func(p Plan) {
		t.Helper()
		for _, in := range p.Inbounds {
			for _, e := range p.Remove {
				if err := api.RemoveUser(ctx, in.Tag, e); err != nil {
					t.Fatalf("RemoveUser %s: %v", in.Tag, err)
				}
			}
			for _, u := range p.Add {
				if err := api.AddUser(ctx, in.Tag, in.Flow, u); err != nil {
					t.Fatalf("AddUser %s: %v", in.Tag, err)
				}
			}
		}
	}
	applyPlan(plan)
	counted := userTraffic(t, api, email)
	_ = request(t, lanPort)
	_ = request(t, tcpLan)
	time.Sleep(500 * time.Millisecond)
	if after := userTraffic(t, api, email); after != counted {
		t.Fatalf("a removed user was still served: %d -> %d bytes", counted, after)
	}

	back, err := Decide(empty, withUser)
	if err != nil || back.Kind != KindLive || len(back.Add) != 1 {
		t.Fatalf("live add plan %+v %v", back, err)
	}
	applyPlan(back)
	_ = request(t, lanPort)
	waitFor(t, "XHTTP traffic after the user came back", func() bool { return userTraffic(t, api, email) > counted })
	viaXHTTP := userTraffic(t, api, email)
	_ = request(t, tcpLan)
	waitFor(t, "TCP traffic after the user came back", func() bool { return userTraffic(t, api, email) > viaXHTTP })

	if lab.hits.Load() != 0 {
		t.Fatalf("the private service was reached %d times through the proxy", lab.hits.Load())
	}
	if out := serverLog.String(); ipv4Like.MatchString(out) {
		t.Fatalf("the server's output shows an address:\n%s", out)
	}
}
