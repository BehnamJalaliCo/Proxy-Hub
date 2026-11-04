package config

import (
	"os"
	"strings"
	"testing"
)

const good = `{
  "panel_url": "https://agent.example.com",
  "node_id": "0b7f6a3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
  "ca_file": "/etc/proxyhub-agent/ca.pem",
  "token_file": "/etc/proxyhub-agent/registration_token",
  "state_dir": "/var/lib/proxyhub-agent",
  "telemt_config": "/etc/telemt/telemt.toml",
  "telemt_data_dir": "/var/lib/telemt",
  "telemt_user": "telemt",
  "metrics_listen": "127.0.0.1:9090",
  "telemt_version": "3.5.16",
  "reload_command": ["systemctl", "reload", "telemt.service"],
  "restart_command": ["systemctl", "restart", "telemt.service"]
}`

func TestParseGood(t *testing.T) {
	c, err := Parse([]byte(good))
	if err != nil {
		t.Fatal(err)
	}
	if c.MetricsURL() != "http://127.0.0.1:9090/metrics" {
		t.Fatalf("metrics url = %s", c.MetricsURL())
	}
	if c.IdentityFile() != "/var/lib/proxyhub-agent/identity.pem" {
		t.Fatal(c.IdentityFile())
	}
}

// The agent.json that node-image's cloud-init template writes must be accepted as is.
func TestCloudInitTemplateConfigParses(t *testing.T) {
	data, err := os.ReadFile("../../../node-image/cloud-init/node-user-data.yaml.tmpl")
	if err != nil {
		t.Skip("node-image not next to the agent module")
	}
	text := string(data)
	start := strings.Index(text, "\n      {\n")
	end := strings.Index(text, "\n      }\n")
	if start < 0 || end < start {
		t.Fatal("agent.json block not found in the template")
	}
	block := text[start+1 : end+8]
	block = strings.ReplaceAll(block, "${PANEL_AGENT_URL}", "https://agent.example.com")
	block = strings.ReplaceAll(block, "${NODE_ID}", "0b7f6a3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b")
	c, err := Parse([]byte(block))
	if err != nil {
		t.Fatalf("template config refused: %v", err)
	}
	if c.TelemtUser != "telemt" || len(c.RestartCommand) == 0 || c.MetricsURL() != "http://127.0.0.1:9090/metrics" {
		t.Fatalf("template config = %+v", c)
	}
}

func TestParseRefuses(t *testing.T) {
	swap := func(old, new string) string {
		if !strings.Contains(good, old) {
			t.Fatalf("fixture lacks %q", old)
		}
		return strings.Replace(good, old, new, 1)
	}
	cases := map[string]string{
		"unknown key":          swap(`"telemt_user": "telemt",`, `"telemt_user": "telemt", "debug": true,`),
		"plain http remote":    swap(`https://agent.example.com`, `http://agent.example.com`),
		"panel url with path":  swap(`https://agent.example.com`, `https://agent.example.com/x`),
		"panel url with user":  swap(`https://agent.example.com`, `https://u:p@agent.example.com`),
		"node id upper":        swap(`0b7f6a3e`, `0B7F6A3E`),
		"relative path":        swap(`"/var/lib/proxyhub-agent"`, `"state"`),
		"metrics public":       swap(`127.0.0.1:9090`, `0.0.0.0:9090`),
		"metrics hostname":     swap(`127.0.0.1:9090`, `localhost:9090`),
		"metrics port missing": swap(`127.0.0.1:9090`, `127.0.0.1`),
	}
	for name, body := range cases {
		if _, err := Parse([]byte(body)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := Parse([]byte(swap(`https://agent.example.com`, `http://127.0.0.1:8000`))); err != nil {
		t.Errorf("loopback http refused: %v", err)
	}
}
