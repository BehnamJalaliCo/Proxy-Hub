package enforce

import (
	"testing"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
)

const (
	secretA = "0123456789abcdef0123456789abcdef"
	secretB = "fedcba9876543210fedcba9876543210"
)

func TestUsageSurvivesTelemtRestarts(t *testing.T) {
	u := NewUsage()
	g := Generation(secretA)
	u.Observe("alice", g, 100)
	u.Observe("alice", g, 250)  // +150
	u.Observe("alice", g, 40)   // Telemt restarted: +40
	u.Observe("alice", g, 1040) // +1000
	if got := u.Total("alice", g); got != 1290 {
		t.Fatalf("total = %d, want 1290", got)
	}
	// A new secret under the same name starts from zero.
	g2 := Generation(secretB)
	u.Observe("alice", g2, 5)
	if u.Total("alice", g2) != 5 || u.Total("alice", g) != 0 {
		t.Fatal("new secret did not reset usage")
	}
	u.Prune(map[string]bool{})
	if len(u.Users) != 0 {
		t.Fatal("prune kept a removed user")
	}
}

func TestGenerationDoesNotRevealSecret(t *testing.T) {
	g := Generation(secretA)
	if len(g) != 16 || g == secretA[:16] {
		t.Fatalf("generation = %q", g)
	}
}

func TestEffectiveDisablesExpiredAndExhausted(t *testing.T) {
	now := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	past := now.Add(-time.Second)
	future := now.Add(time.Hour)
	quota := uint64(1000)
	users := []desired.User{
		{Name: "fresh", Secret: secretA, Enabled: true, ExpiresAt: &future, DataQuotaBytes: &quota},
		{Name: "expired", Secret: secretA, Enabled: true, ExpiresAt: &past},
		{Name: "exactly_now", Secret: secretA, Enabled: true, ExpiresAt: &now},
		{Name: "exhausted", Secret: secretB, Enabled: true, DataQuotaBytes: &quota},
		{Name: "off", Secret: secretB, Enabled: false},
	}
	usage := NewUsage()
	usage.Observe("exhausted", Generation(secretB), 1000)
	usage.Observe("fresh", Generation(secretA), 999)
	out, d := Effective(users, usage, now)
	want := map[string]bool{"fresh": true, "expired": false, "exactly_now": false, "exhausted": false, "off": false}
	for _, u := range out {
		if u.Enabled != want[u.Name] {
			t.Errorf("%s enabled = %t", u.Name, u.Enabled)
		}
	}
	if d.Expired != 2 || d.OverQuota != 1 {
		t.Fatalf("decision = %+v", d)
	}
	if len(out) != len(users) {
		t.Fatal("disabled users must still be rendered (user_enabled = false)")
	}
}
