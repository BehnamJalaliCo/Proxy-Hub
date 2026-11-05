package xray

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
)

func xhttpDesired(t *testing.T) *desired.Xray {
	t.Helper()
	x := testDesired()
	x.XHTTP = &desired.XHTTP{Port: 2083, Path: "/0123456789abcdef"}
	if err := x.Validate([]int{8443}); err != nil {
		t.Fatal(err)
	}
	return x
}

// A REALITY section with an xhttp part renders two inbounds: REALITY on TCP (Vision) first, then
// VLESS + XHTTP + REALITY on its own port with the same REALITY settings and the same users without
// a flow. Log, API, policy, outbounds and routing are shared, so the privacy settings hold for both.
func TestRenderXHTTP(t *testing.T) {
	x := xhttpDesired(t)
	data := render(t, x)
	if err := CheckInvariants(data); err != nil {
		t.Fatal(err)
	}
	m := decode(t, data)
	log := m["log"].(map[string]any)
	if log["access"] != "none" || log["maskAddress"] != "full" || log["dnsLog"] != false || log["loglevel"] != LogLevel {
		t.Fatalf("log %v", log)
	}
	inbounds := m["inbounds"].([]any)
	if len(inbounds) != 2 {
		t.Fatalf("%d inbounds", len(inbounds))
	}
	tcp, xh := inbounds[0].(map[string]any), inbounds[1].(map[string]any)
	if tcp["tag"] != InboundTag || tcp["port"] != float64(443) {
		t.Fatalf("first inbound %v", tcp)
	}
	if xh["tag"] != XHTTPInboundTag || xh["port"] != float64(2083) || xh["listen"] != "0.0.0.0" || xh["protocol"] != "vless" {
		t.Fatalf("xhttp inbound %v", xh)
	}
	if xh["settings"].(map[string]any)["decryption"] != "none" || xh["sniffing"].(map[string]any)["routeOnly"] != true {
		t.Fatalf("xhttp inbound settings %v", xh)
	}
	st := xh["streamSettings"].(map[string]any)
	if st["network"] != "xhttp" || st["security"] != "reality" || st["tlsSettings"] != nil || st["wsSettings"] != nil {
		t.Fatalf("stream %v", st)
	}
	if hs := st["xhttpSettings"].(map[string]any); hs["path"] != "/0123456789abcdef" || hs["mode"] != "auto" || len(hs) != 2 {
		t.Fatalf("xhttpSettings %v", hs)
	}
	same, _ := json.Marshal(tcp["streamSettings"].(map[string]any)["realitySettings"])
	other, _ := json.Marshal(st["realitySettings"])
	if string(same) != string(other) {
		t.Fatalf("the XHTTP inbound has other REALITY settings:\n%s\n%s", same, other)
	}
	tcpClients := tcp["settings"].(map[string]any)["clients"].([]any)
	xhClients := xh["settings"].(map[string]any)["clients"].([]any)
	if len(tcpClients) != 2 || len(xhClients) != 2 {
		t.Fatalf("clients %v %v", tcpClients, xhClients)
	}
	for i := range xhClients {
		a, b := tcpClients[i].(map[string]any), xhClients[i].(map[string]any)
		if a["id"] != b["id"] || a["email"] != b["email"] || a["flow"] != Flow || b["flow"] != "" {
			t.Fatalf("client %d: %v / %v", i, a, b)
		}
	}
	// Without the xhttp part: exactly the one-inbound config of before.
	plain := testDesired()
	if n := len(decode(t, render(t, plain))["inbounds"].([]any)); n != 1 {
		t.Fatalf("%d inbounds without xhttp", n)
	}
	// Never next to ws_tls (the desired state refuses it as well).
	ws := testDesired()
	ws.Transport, ws.XHTTP = desired.TransportWSTLS, &desired.XHTTP{Port: 2083, Path: "/abc"}
	if _, err := Render(ws, nil, "127.0.0.1:10085", TLSFiles{Cert: "/a.crt", Key: "/a.key"}); err == nil {
		t.Fatal("rendered xhttp next to ws_tls")
	}
}

// The invariant check refuses hand-made variations of the two-inbound config.
func TestXHTTPInvariants(t *testing.T) {
	data := render(t, xhttpDesired(t))
	mutate := func(f func(ins []any)) []byte {
		var c map[string]any
		_ = json.Unmarshal(data, &c)
		f(c["inbounds"].([]any))
		out, _ := json.Marshal(c)
		return out
	}
	xh := func(ins []any) map[string]any { return ins[1].(map[string]any) }
	stream := func(ins []any) map[string]any { return xh(ins)["streamSettings"].(map[string]any) }
	for name, bad := range map[string][]byte{
		"plain xhttp":  mutate(func(ins []any) { stream(ins)["security"] = "none"; delete(stream(ins), "realitySettings") }),
		"show reality": mutate(func(ins []any) { stream(ins)["realitySettings"].(map[string]any)["show"] = true }),
		"proxy proto":  mutate(func(ins []any) { stream(ins)["realitySettings"].(map[string]any)["xver"] = 1 }),
		"root path":    mutate(func(ins []any) { stream(ins)["xhttpSettings"].(map[string]any)["path"] = "/" }),
		"packet-up":    mutate(func(ins []any) { stream(ins)["xhttpSettings"].(map[string]any)["mode"] = "packet-up" }),
		"extra key":    mutate(func(ins []any) { stream(ins)["xhttpSettings"].(map[string]any)["xPaddingBytes"] = "1-2" }),
		"vision": mutate(func(ins []any) {
			xh(ins)["settings"].(map[string]any)["clients"].([]any)[0].(map[string]any)["flow"] = Flow
		}),
		"same port":     mutate(func(ins []any) { xh(ins)["port"] = 443 }),
		"loopback only": mutate(func(ins []any) { xh(ins)["listen"] = "127.0.0.1" }),
		"no route only": mutate(func(ins []any) { xh(ins)["sniffing"].(map[string]any)["routeOnly"] = false }),
		"decryption":    mutate(func(ins []any) { xh(ins)["settings"].(map[string]any)["decryption"] = "mlkem768x25519plus" }),
		"swapped":       mutate(func(ins []any) { ins[0], ins[1] = ins[1], ins[0] }),
		"twice":         mutate(func(ins []any) { xh(ins)["tag"] = InboundTag }),
		"three": func() []byte {
			var c map[string]any
			_ = json.Unmarshal(data, &c)
			c["inbounds"] = append(c["inbounds"].([]any), c["inbounds"].([]any)[1])
			out, _ := json.Marshal(c)
			return out
		}(),
	} {
		if err := CheckInvariants(bad); err == nil {
			t.Errorf("%s: accepted", name)
		} else if !strings.Contains(err.Error(), ErrRefused.Error()) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// Adding or removing the XHTTP inbound is a restart; a user-only change goes live in both inbounds
// (Vision in the REALITY one, no flow in the XHTTP one); a changed XHTTP path is a restart.
func TestDecideXHTTP(t *testing.T) {
	plain, withXHTTP := render(t, testDesired()), render(t, xhttpDesired(t))
	for name, pair := range map[string][2][]byte{"add xhttp": {plain, withXHTTP}, "remove xhttp": {withXHTTP, plain}} {
		if p, err := Decide(pair[0], pair[1]); err != nil || p.Kind != KindRestart {
			t.Errorf("%s: %v %v", name, p.Kind, err)
		}
	}
	fewer := xhttpDesired(t)
	fewer.Users = fewer.Users[:1]
	p, err := Decide(withXHTTP, render(t, fewer))
	if err != nil || p.Kind != KindLive || len(p.Remove) != 1 || len(p.Add) != 0 {
		t.Fatalf("live %+v %v", p, err)
	}
	if got := fmt.Sprint(p.Inbounds); got != fmt.Sprint([]LiveInbound{{InboundTag, Flow}, {XHTTPInboundTag, ""}}) {
		t.Fatalf("inbounds %s", got)
	}
	moved := xhttpDesired(t)
	moved.XHTTP.Path = "/fedcba9876543210"
	if p, _ := Decide(withXHTTP, render(t, moved)); p.Kind != KindRestart {
		t.Fatalf("path change: %v", p.Kind)
	}
	// A current file whose inbounds disagree about the users (not ours) is a restart, never live.
	var c map[string]any
	_ = json.Unmarshal(withXHTTP, &c)
	c["inbounds"].([]any)[1].(map[string]any)["settings"].(map[string]any)["clients"] = []any{}
	odd, _ := json.Marshal(c)
	if p, _ := Decide(odd, render(t, fewer)); p.Kind != KindRestart {
		t.Fatalf("inconsistent current file: %v", p.Kind)
	}
}
