package agent

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
	"os/user"
	"strconv"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/enforce"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/report"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/xray"
)

// targetRecheck is how often the REALITY target is checked again from the node.
const targetRecheck = 6 * time.Hour

// downRestart: an Xray whose API does not answer although its config is in place is restarted, at
// most this often (systemd restarts it after a crash already; this covers a clean exit).
const downRestart = 2 * time.Minute

// xrayRuntime is the agent's Xray state. nil on a node without Xray (no settings file).
type xrayRuntime struct {
	settings *xray.Settings
	api      *xray.API
	service  xray.Service
	owner    fsutil.Owner
	// checkTarget is xray.CheckTarget on a node; tests point it at a local TLS server.
	checkTarget func(ctx context.Context, target, serverName string, roots *x509.CertPool) error

	up          bool
	uptimeS     *float64
	applyErr    string
	targetOK    *bool
	targetKey   string
	targetAt    time.Time
	lastRestart time.Time
}

// initXray loads the node's Xray settings. Without the file this node simply serves no Xray; a
// broken file is reported and also leaves Xray alone, so Telemt keeps working.
func (a *Agent) initXray(path string) {
	s, err := xray.LoadSettings(path)
	if errors.Is(err, os.ErrNotExist) {
		return
	}
	if err == nil {
		err = a.SetXray(s)
	}
	if err != nil {
		a.log.Warn("xray settings refused; xray stays unmanaged", "error", err.Error())
	}
}

// SetXray enables Xray management with the given settings.
func (a *Agent) SetXray(s *xray.Settings) error {
	if err := s.Validate(); err != nil {
		return err
	}
	owner := fsutil.KeepOwner
	if s.User != "" {
		u, err := user.Lookup(s.User)
		if err != nil {
			return fmt.Errorf("xray user: %w", err)
		}
		uid, _ := strconv.Atoi(u.Uid)
		gid, _ := strconv.Atoi(u.Gid)
		owner = fsutil.Owner{UID: uid, GID: gid}
	}
	if a.local.XrayUsage == nil {
		a.local.XrayUsage = enforce.NewUsage()
	}
	a.xr = &xrayRuntime{
		settings: s,
		api:      xray.NewAPI(s.API),
		service: xray.Service{
			Binary: s.Binary, AssetsDir: s.AssetsDir,
			RestartCommand: s.RestartCommand, StopCommand: s.StopCommand,
		},
		owner:       owner,
		checkTarget: xray.CheckTarget,
	}
	return nil
}

// ScrapeXray reads Xray's uptime and per-user counters and folds them into the usage tracker.
func (a *Agent) ScrapeXray(ctx context.Context) {
	if a.xr == nil {
		return
	}
	uptime, err := a.xr.api.Uptime(ctx)
	var traffic map[string]uint64
	if err == nil {
		traffic, err = a.xr.api.UserTraffic(ctx)
	}
	if err != nil {
		a.xr.up, a.xr.uptimeS = false, nil
		return
	}
	up := float64(uptime)
	a.xr.up, a.xr.uptimeS = true, &up
	if a.doc == nil || a.doc.Xray == nil {
		return
	}
	keep := map[string]bool{}
	for _, u := range a.doc.Xray.Users {
		keep[u.Email] = true
		if raw, ok := traffic[u.Email]; ok {
			a.local.XrayUsage.Observe(u.Email, enforce.Generation(u.ID), raw)
		}
	}
	a.local.XrayUsage.Prune(keep)
	if err := a.saveLocal(); err != nil {
		a.log.Warn("could not save xray usage", "error", err.Error())
	}
}

// ApplyXray renders the Xray config and brings the running Xray to it: users live when only they
// changed, otherwise a tested file swap and a restart. Without a desired Xray section Xray is
// stopped and its config removed.
func (a *Agent) ApplyXray(ctx context.Context) error {
	if a.xr == nil || a.doc == nil {
		return nil
	}
	err := a.applyXray(ctx)
	if err == nil {
		a.xr.applyErr = ""
	}
	return err
}

func (a *Agent) applyXray(ctx context.Context) error {
	path := a.xr.settings.Config
	if a.doc.Xray == nil {
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err := a.xr.service.Stop(ctx); err != nil {
			a.xr.applyErr = "xray_service_failed"
			return err
		}
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			a.xr.applyErr = "xray_write_failed"
			return err
		}
		a.log.Info("xray stopped: no longer desired")
		return nil
	}
	a.checkXrayTarget(ctx)
	users, decision := xray.Effective(a.doc.Xray.Users, a.local.XrayUsage, a.now())
	if decision.Expired > 0 || decision.OverQuota > 0 {
		a.log.Info("local xray enforcement", "expired", decision.Expired, "over_quota", decision.OverQuota)
	}
	data, err := xray.Render(a.doc.Xray, users, a.xr.settings.API)
	if err != nil {
		a.xr.applyErr = "xray_render_refused"
		return err
	}
	current, err := fsutil.ReadIfExists(path)
	if err != nil {
		a.xr.applyErr = "xray_read_failed"
		return err
	}
	plan, err := xray.Decide(current, data)
	if err != nil {
		a.xr.applyErr = "xray_render_refused"
		return err
	}
	if plan.Kind == xray.KindNone && !a.xr.up && a.now().Sub(a.xr.lastRestart) >= downRestart {
		a.log.Warn("xray is not answering; restarting it")
		a.local.XrayRestartPending = true
	}
	if !a.local.XrayRestartPending {
		switch plan.Kind {
		case xray.KindNone:
			return nil
		case xray.KindLive:
			if err := a.liveUsers(ctx, plan); err == nil {
				if err := xray.WriteConfig(path, data, a.xr.owner); err != nil {
					a.xr.applyErr = "xray_write_failed"
					return err
				}
				a.log.Info("xray users changed live", "added", len(plan.Add), "removed", len(plan.Remove))
				return nil
			}
			a.log.Warn("live xray user change failed; restarting xray with the new config")
		}
	}
	candidate, err := xray.StageConfig(path, data, a.xr.owner)
	if err != nil {
		if errors.Is(err, xray.ErrRefused) {
			a.xr.applyErr = "xray_invariant_refused"
		} else {
			a.xr.applyErr = "xray_write_failed"
		}
		return err
	}
	if err := a.xr.service.Test(ctx, candidate); err != nil {
		_ = os.Remove(candidate)
		a.xr.applyErr = "xray_config_refused"
		return err
	}
	a.local.XrayRestartPending = true
	if err := a.saveLocal(); err != nil {
		_ = os.Remove(candidate)
		a.xr.applyErr = "xray_write_failed"
		return err
	}
	if err := xray.Promote(candidate, path); err != nil {
		a.xr.applyErr = "xray_write_failed"
		return err
	}
	a.xr.lastRestart = a.now()
	if err := a.xr.service.Restart(ctx); err != nil {
		a.xr.applyErr = "xray_service_failed"
		return err
	}
	a.local.XrayRestartPending = false
	if err := a.saveLocal(); err != nil {
		a.log.Warn("could not save local state", "error", err.Error())
	}
	a.log.Info("xray config written", "action", "restart")
	return nil
}

// liveUsers applies a user-only change to the running Xray. Any failure makes the caller restart
// Xray from the file instead, so the running state never stays half-changed.
func (a *Agent) liveUsers(ctx context.Context, plan xray.Plan) error {
	for _, email := range plan.Remove {
		if err := a.xr.api.RemoveUser(ctx, email); err != nil {
			return err
		}
	}
	for _, u := range plan.Add {
		if err := a.xr.api.AddUser(ctx, u); err != nil {
			return err
		}
	}
	return nil
}

// checkXrayTarget re-checks the REALITY target from this node when it changed or every few hours.
func (a *Agent) checkXrayTarget(ctx context.Context) {
	x := a.doc.Xray
	key := x.Target + "|" + x.ServerNames[0]
	if key == a.xr.targetKey && a.now().Sub(a.xr.targetAt) < targetRecheck {
		return
	}
	ok := a.xr.checkTarget(ctx, x.Target, x.ServerNames[0], nil) == nil
	a.xr.targetOK, a.xr.targetKey, a.xr.targetAt = &ok, key, a.now()
	if !ok {
		a.log.Warn("reality target failed the TLS 1.3 + HTTP/2 check from this node")
	}
}

// xrayReport is the report's Xray section (always present from this agent on).
func (a *Agent) xrayReport() *report.Xray {
	if a.xr == nil {
		return report.BuildXray(report.XrayInput{})
	}
	totals := map[string]uint64{}
	if a.doc != nil && a.doc.Xray != nil {
		for _, u := range a.doc.Xray.Users {
			totals[u.Email] = a.local.XrayUsage.Total(u.Email, enforce.Generation(u.ID))
		}
	}
	return report.BuildXray(report.XrayInput{
		Enabled:    true,
		Up:         a.xr.up,
		Version:    a.xr.settings.Version,
		UptimeS:    a.xr.uptimeS,
		TargetOK:   a.xr.targetOK,
		ApplyError: a.xr.applyErr,
		Totals:     totals,
	})
}
