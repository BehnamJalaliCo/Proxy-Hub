package telemt

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os/exec"
	"sort"
	"strings"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
)

// WriteConfig writes a rendered config atomically (temp file + rename, mode 0600). It re-checks
// the invariants on the exact bytes first and refuses to write anything that breaks them.
func WriteConfig(path string, data []byte, owner fsutil.Owner) error {
	if err := CheckInvariants(data); err != nil {
		return err
	}
	return fsutil.WriteAtomic(path, data, 0o600, owner)
}

// Action is what the running Telemt needs after a config change.
type Action int

// Actions, from cheapest to most disruptive.
const (
	ActionNone    Action = iota // file unchanged
	ActionReload                // only hot-reloadable keys changed (users, tags, limits, log level)
	ActionRestart               // a restart-only key changed (ports, TLS domain, modes, ...) or first start
)

func (a Action) String() string {
	return [...]string{"none", "reload", "restart"}[a]
}

// Keys Telemt 3.5.16 applies without restart (src/config/hot_reload.rs): every [access.*] user and
// quota map, general.ad_tag, general.log_level and [general.telemetry].
func hotPath(table, key string) bool {
	switch {
	case table == "access" || strings.HasPrefix(table, "access."):
		return true
	case table == "general" && (key == "ad_tag" || key == "log_level"):
		return true
	case table == "general.telemetry":
		return true
	}
	return false
}

// restartKey summarizes everything that needs a restart to take effect.
func restartKey(doc *Doc) string {
	var lines []string
	for name, t := range doc.Tables {
		for _, k := range t.Keys {
			if !hotPath(name, k) {
				lines = append(lines, fmt.Sprintf("%s.%s=%#v", name, k, t.Values[k]))
			}
		}
	}
	for name, tables := range doc.Arrays {
		for i, t := range tables {
			for _, k := range t.Keys {
				lines = append(lines, fmt.Sprintf("%s[%d].%s=%#v", name, i, k, t.Values[k]))
			}
		}
	}
	sort.Strings(lines)
	sum := sha256.Sum256([]byte(strings.Join(lines, "\n")))
	return hex.EncodeToString(sum[:])
}

// Decide compares the current file (nil if absent) with the new one.
func Decide(current, next []byte) (Action, error) {
	if bytes.Equal(current, next) {
		return ActionNone, nil
	}
	nextDoc, err := ParseSubset(next)
	if err != nil {
		return ActionNone, err
	}
	if current == nil {
		return ActionRestart, nil
	}
	curDoc, err := ParseSubset(current)
	if err != nil { // a file we did not write (tampered or foreign): start clean
		return ActionRestart, nil
	}
	if restartKey(curDoc) != restartKey(nextDoc) {
		return ActionRestart, nil
	}
	return ActionReload, nil
}

// Service runs the configured reload/restart commands (systemctl on a node). Telemt also watches
// its config file itself; the explicit reload (SIGHUP via systemd) makes the timing deterministic.
type Service struct {
	ReloadCommand  []string
	RestartCommand []string
}

// Do performs the action. Command output is discarded: nothing from Telemt is logged.
func (s Service) Do(ctx context.Context, a Action) error {
	var argv []string
	switch a {
	case ActionNone:
		return nil
	case ActionReload:
		argv = s.ReloadCommand
	case ActionRestart:
		argv = s.RestartCommand
	}
	if len(argv) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...) //nolint:gosec // fixed argv from the agent's own config file
	if err := cmd.Run(); err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return fmt.Errorf("telemt %s: exit status %d", a, exit.ExitCode())
		}
		return fmt.Errorf("telemt %s: %w", a, err)
	}
	return nil
}
