package agent

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/enforce"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/report"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/xray"
)

// targetRecheck is how often the REALITY target (or a passed own-TLS check) is checked again.
const targetRecheck = 6 * time.Hour

// ownTLSRetry: a failed own-TLS check is repeated this often (it is local and cheap; right after a
// restart Xray may simply not listen yet).
const ownTLSRetry = time.Minute

// downRestart: an Xray whose API does not answer although its config is in place is restarted, at
// most this often (systemd restarts it after a crash already; this covers a clean exit).
const downRestart = 2 * time.Minute

// Certificate files: readable by the Xray user (owner) and group, nobody else.
const (
	certFileMode = 0o640
	certDirMode  = 0o750
)

// xrayRuntime is the agent's Xray state. nil on a node without Xray (no settings file).
type xrayRuntime struct {
	settings *xray.Settings
	api      *xray.API
	service  xray.Service
	owner    fsutil.Owner
	// checkTarget is xray.CheckTarget on a node; tests point it at a local TLS server.
	checkTarget func(ctx context.Context, target, serverName string, roots *x509.CertPool) error
	// checkOwnTLS is xray.CheckOwnTLS; roots nil means the system roots (Let's Encrypt's are there).
	checkOwnTLS func(ctx context.Context, port int, serverName string, roots *x509.CertPool) error
	roots       *x509.CertPool

	up       bool
	uptimeS  *float64
	applyErr string
	targetOK *bool
	// targetChecked / sniChecked: what targetOK is about (the report says so). REALITY: the target
	// and its server name; ws_tls: "<own name>:<port>" and the own name. certChecked: the
	// certificate the own-TLS check saw (a renewed one is checked again).
	targetChecked string
	sniChecked    string
	certChecked   string
	targetAt      time.Time
	lastRestart   time.Time
	// xhttpPort: the port of the VLESS + XHTTP + REALITY inbound in the config last applied without
	// an error (0: none). Reported while Xray is up, so the panel publishes XHTTP links only for a
	// node that really serves them.
	xhttpPort int
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
		checkOwnTLS: xray.CheckOwnTLS,
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
// stopped and its config removed. With ws_tls, the certificate files are written first, those of
// an older certificate are removed once the running config no longer names them, and the inbound
// is checked from the node itself once Xray runs it.
func (a *Agent) ApplyXray(ctx context.Context) error {
	if a.xr == nil || a.doc == nil {
		return nil
	}
	files, kind, err := a.applyXray(ctx)
	if err != nil {
		return err
	}
	a.xr.applyErr = ""
	a.xr.xhttpPort = 0
	if x := a.doc.Xray; x != nil && x.XHTTP != nil && !x.WSTLS() {
		a.xr.xhttpPort = x.XHTTP.Port
	}
	if err := a.pruneCertFiles(files); err != nil {
		a.log.Warn("could not remove old certificate files", "error", err.Error())
	}
	if a.doc.Xray != nil && a.doc.Xray.WSTLS() && kind == xray.KindNone && a.xr.up {
		a.checkOwnInbound(ctx, files)
	}
	return nil
}

func (a *Agent) applyXray(ctx context.Context) (xray.TLSFiles, xray.Kind, error) {
	var files xray.TLSFiles
	path := a.xr.settings.Config
	if a.doc.Xray == nil {
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			return files, xray.KindNone, nil
		}
		if err := a.xr.service.Stop(ctx); err != nil {
			a.xr.applyErr = "xray_service_failed"
			return files, xray.KindNone, err
		}
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			a.xr.applyErr = "xray_write_failed"
			return files, xray.KindNone, err
		}
		a.log.Info("xray stopped: no longer desired")
		return files, xray.KindRestart, nil
	}
	x := a.doc.Xray
	if x.WSTLS() {
		var err error
		if files, err = a.writeCertFiles(x); err != nil {
			a.xr.applyErr = "xray_write_failed"
			return files, xray.KindNone, err
		}
	} else {
		a.checkXrayTarget(ctx)
	}
	users, decision := xray.Effective(x.Users, a.local.XrayUsage, a.now())
	if decision.Expired > 0 || decision.OverQuota > 0 {
		a.log.Info("local xray enforcement", "expired", decision.Expired, "over_quota", decision.OverQuota)
	}
	data, err := xray.Render(x, users, a.xr.settings.API, files)
	if err != nil {
		a.xr.applyErr = "xray_render_refused"
		return files, xray.KindNone, err
	}
	current, err := fsutil.ReadIfExists(path)
	if err != nil {
		a.xr.applyErr = "xray_read_failed"
		return files, xray.KindNone, err
	}
	plan, err := xray.Decide(current, data)
	if err != nil {
		a.xr.applyErr = "xray_render_refused"
		return files, xray.KindNone, err
	}
	if plan.Kind == xray.KindNone && !a.xr.up && a.now().Sub(a.xr.lastRestart) >= downRestart {
		a.log.Warn("xray is not answering; restarting it")
		a.local.XrayRestartPending = true
	}
	if !a.local.XrayRestartPending {
		switch plan.Kind {
		case xray.KindNone:
			return files, xray.KindNone, nil
		case xray.KindLive:
			if err := a.liveUsers(ctx, plan); err == nil {
				if err := xray.WriteConfig(path, data, a.xr.owner); err != nil {
					a.xr.applyErr = "xray_write_failed"
					return files, xray.KindLive, err
				}
				a.log.Info("xray users changed live", "added", len(plan.Add), "removed", len(plan.Remove))
				return files, xray.KindLive, nil
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
		return files, xray.KindRestart, err
	}
	if err := a.xr.service.Test(ctx, candidate); err != nil {
		_ = os.Remove(candidate)
		a.xr.applyErr = "xray_config_refused"
		return files, xray.KindRestart, err
	}
	a.local.XrayRestartPending = true
	if err := a.saveLocal(); err != nil {
		_ = os.Remove(candidate)
		a.xr.applyErr = "xray_write_failed"
		return files, xray.KindRestart, err
	}
	if err := xray.Promote(candidate, path); err != nil {
		a.xr.applyErr = "xray_write_failed"
		return files, xray.KindRestart, err
	}
	a.xr.lastRestart = a.now()
	if err := a.xr.service.Restart(ctx); err != nil {
		a.xr.applyErr = "xray_service_failed"
		return files, xray.KindRestart, err
	}
	a.local.XrayRestartPending = false
	if err := a.saveLocal(); err != nil {
		a.log.Warn("could not save local state", "error", err.Error())
	}
	a.log.Info("xray config written", "action", "restart")
	return files, xray.KindRestart, nil
}

// certStem names a certificate's files by their content: a renewed certificate gets new names, so
// the config changes with it and Xray restarts onto it (the config itself names files only).
func certStem(x *desired.Xray) string {
	sum := sha256.Sum256([]byte(x.TLSCertChain + "\x00" + x.TLSKey))
	return hex.EncodeToString(sum[:8])
}

// writeCertFiles writes the chain and key of a ws_tls section (0640, owned by the Xray user) under
// the certificate directory, unless they are there already. Nothing about them is logged.
func (a *Agent) writeCertFiles(x *desired.Xray) (xray.TLSFiles, error) {
	dir := a.xr.settings.CertDir()
	if err := os.MkdirAll(dir, certDirMode); err != nil {
		return xray.TLSFiles{}, fmt.Errorf("certificate dir: %w", err)
	}
	if err := os.Chmod(dir, certDirMode); err != nil {
		return xray.TLSFiles{}, fmt.Errorf("certificate dir mode: %w", err)
	}
	if a.xr.owner.GID >= 0 {
		if err := os.Chown(dir, -1, a.xr.owner.GID); err != nil {
			return xray.TLSFiles{}, fmt.Errorf("certificate dir owner: %w", err)
		}
	}
	stem := certStem(x)
	files := xray.TLSFiles{Cert: filepath.Join(dir, stem+".crt"), Key: filepath.Join(dir, stem+".key")}
	for path, content := range map[string]string{files.Cert: x.TLSCertChain, files.Key: x.TLSKey} {
		have, err := fsutil.ReadIfExists(path)
		if err != nil {
			return files, fmt.Errorf("read certificate file: %w", err)
		}
		if have != nil && bytes.Equal(have, []byte(content)) {
			continue
		}
		if err := fsutil.WriteAtomic(path, []byte(content), certFileMode, a.xr.owner); err != nil {
			return files, fmt.Errorf("write certificate file: %w", err)
		}
	}
	return files, nil
}

// pruneCertFiles removes certificate files other than keep (none kept: every one goes), once the
// config Xray runs no longer names them. Only *.crt and *.key files of the directory are touched.
func (a *Agent) pruneCertFiles(keep xray.TLSFiles) error {
	entries, err := os.ReadDir(a.xr.settings.CertDir())
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !(strings.HasSuffix(name, ".crt") || strings.HasSuffix(name, ".key")) {
			continue
		}
		path := filepath.Join(a.xr.settings.CertDir(), name)
		if path == keep.Cert || path == keep.Key {
			continue
		}
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}

// liveUsers applies a user-only change to the running Xray. Any failure makes the caller restart
// Xray from the file instead, so the running state never stays half-changed.
func (a *Agent) liveUsers(ctx context.Context, plan xray.Plan) error {
	for _, in := range plan.Inbounds {
		for _, email := range plan.Remove {
			if err := a.xr.api.RemoveUser(ctx, in.Tag, email); err != nil {
				return err
			}
		}
		for _, u := range plan.Add {
			if err := a.xr.api.AddUser(ctx, in.Tag, in.Flow, u); err != nil {
				return err
			}
		}
	}
	return nil
}

// checkXrayTarget re-checks the REALITY target from this node when it changed or every few hours.
func (a *Agent) checkXrayTarget(ctx context.Context) {
	x := a.doc.Xray
	target, sni := x.Target, x.ServerNames[0]
	if target == a.xr.targetChecked && sni == a.xr.sniChecked && a.xr.certChecked == "" &&
		a.now().Sub(a.xr.targetAt) < targetRecheck {
		return
	}
	ok := a.xr.checkTarget(ctx, target, sni, nil) == nil
	a.xr.targetOK, a.xr.targetChecked, a.xr.sniChecked, a.xr.certChecked, a.xr.targetAt = &ok, target, sni, "", a.now()
	if !ok {
		a.log.Warn("reality target failed the TLS 1.3 + HTTP/2 check from this node")
	}
}

// checkOwnInbound checks the running ws_tls inbound from this node (a TLS handshake on loopback for
// the node's own name, verified with the public roots): when name, port or certificate changed, a
// minute after a failure, and every few hours after a success.
func (a *Agent) checkOwnInbound(ctx context.Context, files xray.TLSFiles) {
	x := a.doc.Xray
	target := x.ServerName + ":" + strconv.Itoa(x.Port)
	cert := filepath.Base(files.Cert)
	same := target == a.xr.targetChecked && x.ServerName == a.xr.sniChecked && cert == a.xr.certChecked
	if same && a.xr.targetOK != nil {
		wait := targetRecheck
		if !*a.xr.targetOK {
			wait = ownTLSRetry
		}
		if a.now().Sub(a.xr.targetAt) < wait {
			return
		}
	}
	ok := a.xr.checkOwnTLS(ctx, x.Port, x.ServerName, a.xr.roots) == nil
	a.xr.targetOK, a.xr.targetChecked, a.xr.sniChecked, a.xr.certChecked, a.xr.targetAt = &ok, target, x.ServerName, cert, a.now()
	if !ok {
		a.log.Warn("the node's own ws_tls inbound failed its TLS check")
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
	// A check about the other transport (the panel switched) is not reported as this one's.
	targetOK, checkedTarget, checkedSNI := a.xr.targetOK, a.xr.targetChecked, a.xr.sniChecked
	if a.doc != nil && a.doc.Xray != nil && a.doc.Xray.WSTLS() != (a.xr.certChecked != "") {
		targetOK, checkedTarget, checkedSNI = nil, "", ""
	}
	return report.BuildXray(report.XrayInput{
		Enabled:           true,
		Up:                a.xr.up,
		Version:           a.xr.settings.Version,
		UptimeS:           a.xr.uptimeS,
		TargetOK:          targetOK,
		CheckedTarget:     checkedTarget,
		CheckedServerName: checkedSNI,
		ApplyError:        a.xr.applyErr,
		XHTTPPort:         a.xr.xhttpPort,
		Totals:            totals,
	})
}
