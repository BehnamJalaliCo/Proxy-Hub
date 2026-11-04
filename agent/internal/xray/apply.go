package xray

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
)

// Kind is what the running Xray needs after a config change.
type Kind int

// Kinds, from cheapest to most disruptive.
const (
	KindNone    Kind = iota // file unchanged
	KindLive                // only users changed: AlterInbound add/remove, then the file
	KindRestart             // anything else changed, or no (readable) current file
)

func (k Kind) String() string { return [...]string{"none", "live", "restart"}[k] }

// Plan says how to get from the current config to the next one.
type Plan struct {
	Kind   Kind
	Add    []User   // KindLive: users to add
	Remove []string // KindLive: labels to remove (a changed id is a remove plus an add)
}

// Decide compares the current file (nil if absent) with the next rendered config.
func Decide(current, next []byte) (Plan, error) {
	if bytes.Equal(current, next) {
		return Plan{Kind: KindNone}, nil
	}
	var nextCfg config
	if err := json.Unmarshal(next, &nextCfg); err != nil || len(nextCfg.Inbounds) != 1 {
		return Plan{}, refuse("next config unreadable")
	}
	var curCfg config
	if current == nil || json.Unmarshal(current, &curCfg) != nil || len(curCfg.Inbounds) != 1 {
		return Plan{Kind: KindRestart}, nil // first start, or a file we did not write
	}
	curClients, nextClients := curCfg.Inbounds[0].Settings.Clients, nextCfg.Inbounds[0].Settings.Clients
	curCfg.Inbounds[0].Settings.Clients, nextCfg.Inbounds[0].Settings.Clients = nil, nil
	a, errA := json.Marshal(curCfg)
	b, errB := json.Marshal(nextCfg)
	if errA != nil || errB != nil || !bytes.Equal(a, b) {
		return Plan{Kind: KindRestart}, nil
	}
	have := map[string]string{}
	for _, c := range curClients {
		have[c.Email] = c.ID
	}
	want := map[string]string{}
	for _, c := range nextClients {
		want[c.Email] = c.ID
	}
	var plan Plan
	plan.Kind = KindLive
	for _, c := range curClients {
		if id, ok := want[c.Email]; !ok || id != c.ID {
			plan.Remove = append(plan.Remove, c.Email)
		}
	}
	for _, c := range nextClients {
		if id, ok := have[c.Email]; !ok || id != c.ID {
			plan.Add = append(plan.Add, User{ID: c.ID, Email: c.Email})
		}
	}
	return plan, nil
}

// Service runs Xray's commands on the node (systemctl, and the pinned binary's config test).
type Service struct {
	Binary         string
	AssetsDir      string
	RestartCommand []string
	StopCommand    []string
}

// Test runs "xray run -test" on a config file with the node's assets: the pinned binary itself
// must accept the config before a restart may use it. Output is discarded (it can quote config).
func (s Service) Test(ctx context.Context, path string) error {
	if s.Binary == "" {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.Binary, "run", "-test", "-config", path) //nolint:gosec // pinned binary from the agent's settings
	cmd.Env = []string{"XRAY_LOCATION_ASSET=" + s.AssetsDir, "PATH=/usr/sbin:/usr/bin:/sbin:/bin"}
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return fmt.Errorf("xray refused the config: exit status %d", exit.ExitCode())
		}
		return fmt.Errorf("xray config test: %w", err)
	}
	return nil
}

// Restart (re)starts xray.service.
func (s Service) Restart(ctx context.Context) error { return run(ctx, "restart", s.RestartCommand) }

// Stop stops xray.service.
func (s Service) Stop(ctx context.Context) error { return run(ctx, "stop", s.StopCommand) }

func run(ctx context.Context, what string, argv []string) error {
	if len(argv) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...) //nolint:gosec // fixed argv from the agent's settings file
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return fmt.Errorf("xray %s: exit status %d", what, exit.ExitCode())
		}
		return fmt.Errorf("xray %s: %w", what, err)
	}
	return nil
}

// WriteConfig re-checks the invariants on the exact bytes, then writes them atomically (0600).
func WriteConfig(path string, data []byte, owner fsutil.Owner) error {
	if err := CheckInvariants(data); err != nil {
		return err
	}
	return fsutil.WriteAtomic(path, data, 0o600, owner)
}

// StageConfig writes a candidate next to the config (same checks and mode) for "xray run -test"
// before it replaces the live file with Promote.
func StageConfig(path string, data []byte, owner fsutil.Owner) (string, error) {
	candidate := filepath.Join(filepath.Dir(path), ".candidate-"+filepath.Base(path))
	if err := WriteConfig(candidate, data, owner); err != nil {
		return "", err
	}
	return candidate, nil
}

// Promote moves a tested candidate over the live config.
func Promote(candidate, path string) error {
	if err := os.Rename(candidate, path); err != nil {
		_ = os.Remove(candidate)
		return fmt.Errorf("promote xray config: %w", err)
	}
	return nil
}
