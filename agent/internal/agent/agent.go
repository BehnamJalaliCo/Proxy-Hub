// Package agent ties the node agent together: one-time registration, certificate renewal, the
// desired-state pull with ETag and backoff, local enforcement, Telemt config apply, metrics scrape
// and the periodic report.
//
// The agent keeps working while the panel is unreachable: it applies the last desired state it
// stored (0600, it contains the Telemt secrets) and keeps enforcing expiry and quota on its own.
// Logs are short operational lines; no token, key, secret, request body or Telemt output is ever
// written to them, and nothing about end users exists to be logged.
package agent

import (
	"context"
	"crypto/ecdsa"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/user"
	"strconv"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/desired"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/enforce"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/metrics"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/panel"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/pki"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/report"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/sysstat"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/telemt"
)

// Agent is one node agent.
type Agent struct {
	cfg      *config.Config
	version  string
	log      *slog.Logger
	client   *panel.Client
	scraper  *http.Client
	service  telemt.Service
	owner    fsutil.Owner
	pinnedCA []byte
	procRoot string
	now      func() time.Time

	identity *pki.Identity
	etag     string
	doc      *desired.Document
	local    localState

	appliedETag string
	applyErr    string
	pending     telemt.Action
	telemtUp    bool
	stats       metrics.Stats
}

// localState is node-only bookkeeping, persisted as local.json (0600).
type localState struct {
	// PlaceholderSecret is used for the disabled placeholder user when no user may be served.
	PlaceholderSecret string         `json:"placeholder_secret"`
	Usage             *enforce.Usage `json:"usage"`
}

// cachedDesired is desired.json: the last document the panel served, with its ETag.
type cachedDesired struct {
	ETag string          `json:"etag"`
	Body json.RawMessage `json:"body"`
}

// New prepares an agent from its config; it does not contact the panel.
func New(cfg *config.Config, version string, log *slog.Logger) (*Agent, error) {
	pinned, err := os.ReadFile(cfg.CAFile)
	if err != nil {
		return nil, fmt.Errorf("read panel CA: %w", err)
	}
	if _, err := pki.ParseCertificatePEM(pinned); err != nil {
		return nil, fmt.Errorf("panel CA: %w", err)
	}
	client, err := panel.New(cfg.PanelURL, cfg.ServerCAFile, version)
	if err != nil {
		return nil, err
	}
	owner := fsutil.KeepOwner
	if cfg.TelemtUser != "" {
		u, err := user.Lookup(cfg.TelemtUser)
		if err != nil {
			return nil, fmt.Errorf("telemt user: %w", err)
		}
		uid, _ := strconv.Atoi(u.Uid)
		gid, _ := strconv.Atoi(u.Gid)
		owner = fsutil.Owner{UID: uid, GID: gid}
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		return nil, fmt.Errorf("state dir: %w", err)
	}
	a := &Agent{
		cfg:      cfg,
		version:  version,
		log:      log,
		client:   client,
		scraper:  &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: 10 * time.Second},
		service:  telemt.Service{ReloadCommand: cfg.ReloadCommand, RestartCommand: cfg.RestartCommand},
		owner:    owner,
		pinnedCA: pinned,
		procRoot: "/proc",
		now:      time.Now,
	}
	if id, err := pki.LoadIdentity(cfg.IdentityFile()); err == nil {
		a.identity = id
		client.SetCertificate(&id.TLS)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("identity: %w", err)
	}
	if err := a.loadState(); err != nil {
		return nil, err
	}
	return a, nil
}

func (a *Agent) loadState() error {
	a.local = localState{Usage: enforce.NewUsage()}
	if data, err := fsutil.ReadIfExists(a.cfg.UsageFile()); err != nil {
		return fmt.Errorf("local state: %w", err)
	} else if data != nil {
		if err := json.Unmarshal(data, &a.local); err != nil {
			a.log.Warn("local state unreadable; starting usage from zero")
			a.local = localState{Usage: enforce.NewUsage()}
		}
		if a.local.Usage == nil {
			a.local.Usage = enforce.NewUsage()
		}
	}
	if len(a.local.PlaceholderSecret) != 32 {
		buf := make([]byte, 16)
		if _, err := rand.Read(buf); err != nil {
			return err
		}
		a.local.PlaceholderSecret = hex.EncodeToString(buf)
		if err := a.saveLocal(); err != nil {
			return err
		}
	}
	data, err := fsutil.ReadIfExists(a.cfg.DesiredFile())
	if err != nil || data == nil {
		return err
	}
	var cached cachedDesired
	if err := json.Unmarshal(data, &cached); err != nil {
		a.log.Warn("cached desired state unreadable; waiting for the panel")
		return nil
	}
	doc, err := desired.Decode(cached.Body, a.cfg.NodeID)
	if err != nil {
		a.log.Warn("cached desired state refused; waiting for the panel")
		return nil
	}
	a.doc, a.etag = doc, cached.ETag
	return nil
}

func (a *Agent) saveLocal() error {
	data, err := json.Marshal(a.local)
	if err != nil {
		return err
	}
	return fsutil.WriteAtomic(a.cfg.UsageFile(), data, 0o600, fsutil.KeepOwner)
}

// Registered reports whether the node has an identity.
func (a *Agent) Registered() bool { return a.identity != nil }

// Register runs the one-time registration: a fresh key and CSR, the token from the token file,
// and a certificate that must chain to the provisioned CA. The token file is removed afterwards.
func (a *Agent) Register(ctx context.Context) error {
	st, err := os.Stat(a.cfg.TokenFile)
	if err != nil {
		return fmt.Errorf("registration token: %w", err)
	}
	if st.Mode().Perm()&0o077 != 0 {
		return errors.New("registration token file must be 0600")
	}
	raw, err := os.ReadFile(a.cfg.TokenFile)
	if err != nil {
		return fmt.Errorf("registration token: %w", err)
	}
	token := string(trimSpace(raw))
	key, err := pki.GenerateKey()
	if err != nil {
		return err
	}
	csr, err := pki.CreateCSR(key, a.cfg.NodeID)
	if err != nil {
		return err
	}
	var telemtVersion *string
	if a.cfg.TelemtVersion != "" {
		v := a.cfg.TelemtVersion
		telemtVersion = &v
	}
	resp, err := a.client.Register(ctx, panel.RegisterRequest{
		Token: token, CSRPEM: string(csr), AgentVersion: a.version, TelemtVersion: telemtVersion,
	})
	if err != nil {
		return fmt.Errorf("register: %w", err)
	}
	if err := a.install(key, resp); err != nil {
		return fmt.Errorf("register: %w", err)
	}
	if err := os.Remove(a.cfg.TokenFile); err != nil && !errors.Is(err, os.ErrNotExist) {
		a.log.Warn("could not remove the used registration token file")
	}
	a.log.Info("registered", "not_after", a.identity.Cert.NotAfter.UTC().Format(time.RFC3339))
	return nil
}

func (a *Agent) install(key *ecdsa.PrivateKey, resp *panel.CertificateResponse) error {
	if _, err := pki.CheckIssued([]byte(resp.CertPEM), []byte(resp.CAPEM), a.pinnedCA, key, a.cfg.NodeID, a.now()); err != nil {
		return err
	}
	if err := pki.SaveIdentity(a.cfg.IdentityFile(), key, []byte(resp.CertPEM)); err != nil {
		return err
	}
	id, err := pki.LoadIdentity(a.cfg.IdentityFile())
	if err != nil {
		return err
	}
	a.identity = id
	a.client.SetCertificate(&id.TLS)
	return nil
}

// RenewIfDue replaces the certificate (with a new key) from two thirds of its lifetime on.
func (a *Agent) RenewIfDue(ctx context.Context) error {
	if a.identity == nil || a.now().Before(pki.RenewAt(a.identity.Cert)) {
		return nil
	}
	key, err := pki.GenerateKey()
	if err != nil {
		return err
	}
	csr, err := pki.CreateCSR(key, a.cfg.NodeID)
	if err != nil {
		return err
	}
	resp, err := a.client.Renew(ctx, string(csr))
	if err != nil {
		return fmt.Errorf("renew: %w", err)
	}
	if err := a.install(key, resp); err != nil {
		return fmt.Errorf("renew: %w", err)
	}
	a.log.Info("certificate renewed", "not_after", a.identity.Cert.NotAfter.UTC().Format(time.RFC3339))
	return nil
}

// Pull fetches the desired state (ETag-conditional) and caches it.
func (a *Agent) Pull(ctx context.Context) error {
	if a.identity == nil {
		return errors.New("not registered")
	}
	body, etag, err := a.client.Desired(ctx, a.etag)
	if err != nil {
		return err
	}
	if body == nil { // 304: unchanged
		return nil
	}
	doc, err := desired.Decode(body, a.cfg.NodeID)
	if err != nil {
		a.applyErr = "desired_invalid"
		return err
	}
	data, err := json.Marshal(cachedDesired{ETag: etag, Body: body})
	if err != nil {
		return err
	}
	if err := fsutil.WriteAtomic(a.cfg.DesiredFile(), data, 0o600, fsutil.KeepOwner); err != nil {
		return fmt.Errorf("cache desired state: %w", err)
	}
	a.doc, a.etag = doc, etag
	return nil
}

// Scrape reads Telemt's counters and folds them into the local usage tracker.
func (a *Agent) Scrape(ctx context.Context) {
	stats, err := metrics.Scrape(ctx, a.scraper, a.cfg.MetricsURL())
	if err != nil {
		a.telemtUp = false
		a.stats = metrics.Stats{}
		return
	}
	a.telemtUp, a.stats = true, stats
	if a.doc == nil || a.doc.Telemt == nil {
		return
	}
	keep := map[string]bool{}
	for _, u := range a.doc.Telemt.Users {
		keep[u.Name] = true
		if s, ok := stats.Users[u.Name]; ok {
			a.local.Usage.Observe(u.Name, enforce.Generation(u.Secret), s.TotalOctets())
		}
	}
	a.local.Usage.Prune(keep)
	if err := a.saveLocal(); err != nil {
		a.log.Warn("could not save usage", "error", err.Error())
	}
}

// Apply renders the effective config, writes it if it changed, and reloads or restarts Telemt.
func (a *Agent) Apply(ctx context.Context) error {
	if a.doc == nil {
		return nil // nothing desired yet
	}
	if a.doc.Telemt == nil {
		a.appliedETag, a.applyErr = a.etag, ""
		return nil
	}
	users, decision := enforce.Effective(a.doc.Telemt.Users, a.local.Usage, a.now())
	if decision.Expired > 0 || decision.OverQuota > 0 {
		a.log.Info("local enforcement", "expired", decision.Expired, "over_quota", decision.OverQuota)
	}
	if len(users) == 0 {
		users = []telemt.User{{Name: desired.PlaceholderUser, Secret: a.local.PlaceholderSecret, Enabled: false}}
	}
	data, err := telemt.Render(a.doc.Telemt, users, telemt.Settings{DataDir: a.cfg.TelemtDataDir, MetricsListen: a.cfg.MetricsListen})
	if err != nil {
		a.applyErr = "render_refused"
		return err
	}
	current, err := fsutil.ReadIfExists(a.cfg.TelemtConfig)
	if err != nil {
		a.applyErr = "read_failed"
		return err
	}
	action, err := telemt.Decide(current, data)
	if err != nil {
		a.applyErr = "render_refused"
		return err
	}
	if action != telemt.ActionNone {
		if err := telemt.WriteConfig(a.cfg.TelemtConfig, data, a.owner); err != nil {
			if errors.Is(err, telemt.ErrRefused) {
				a.applyErr = "invariant_refused"
			} else {
				a.applyErr = "write_failed"
			}
			return err
		}
		a.log.Info("telemt config written", "action", action.String())
	}
	if action > a.pending {
		a.pending = action
	}
	if err := a.service.Do(ctx, a.pending); err != nil {
		a.applyErr = "service_failed"
		return err
	}
	a.pending = telemt.ActionNone
	a.appliedETag, a.applyErr = a.etag, ""
	return nil
}

// Report sends the latest counters to the panel.
func (a *Agent) Report(ctx context.Context) error {
	if a.identity == nil {
		return errors.New("not registered")
	}
	r := report.Build(report.Input{
		AgentVersion: a.version,
		AppliedETag:  a.appliedETag,
		ApplyOK:      a.applyErr == "",
		ApplyError:   a.applyErr,
		TelemtUp:     a.telemtUp,
		Stats:        a.stats,
		System:       sysstat.Read(a.procRoot),
	})
	return a.client.Report(ctx, r)
}

// Once runs one full cycle. It returns the first error but always tries every step, so a panel
// outage still leaves the node enforcing its cached state.
func (a *Agent) Once(ctx context.Context) error {
	var first error
	keep := func(err error) {
		if err != nil && first == nil {
			first = err
		}
	}
	if a.identity == nil {
		if err := a.Register(ctx); err != nil {
			return err
		}
	}
	keep(a.RenewIfDue(ctx))
	keep(a.Pull(ctx))
	a.Scrape(ctx)
	keep(a.Apply(ctx))
	keep(a.Report(ctx))
	return first
}

// Run registers if needed and then loops until ctx ends.
func (a *Agent) Run(ctx context.Context) error {
	for a.identity == nil {
		if err := a.Register(ctx); err != nil {
			a.log.Warn("registration failed; retrying", "error", err.Error())
			if !sleep(ctx, 30*time.Second) {
				return ctx.Err()
			}
			continue
		}
	}
	backoff := newBackoff(2*time.Second, 5*time.Minute)
	nextReport := a.now()
	for {
		if err := a.RenewIfDue(ctx); err != nil {
			a.log.Warn("renewal failed", "error", err.Error())
		}
		wait := a.pollInterval()
		if err := a.Pull(ctx); err != nil {
			wait = backoff.next()
			a.log.Warn("desired state unavailable; enforcing cached state", "error", err.Error(), "retry_in", wait.String())
		} else {
			backoff.reset()
		}
		a.Scrape(ctx)
		if err := a.Apply(ctx); err != nil {
			a.log.Warn("apply failed", "code", a.applyErr, "error", err.Error())
		}
		if !a.now().Before(nextReport) {
			if err := a.Report(ctx); err != nil {
				a.log.Warn("report failed", "error", err.Error())
			}
			nextReport = a.now().Add(a.reportInterval())
		}
		if !sleep(ctx, jitter(wait)) {
			return ctx.Err()
		}
	}
}

func (a *Agent) pollInterval() time.Duration {
	if a.doc != nil {
		return time.Duration(a.doc.Intervals.PollS) * time.Second
	}
	return 15 * time.Second
}

func (a *Agent) reportInterval() time.Duration {
	if a.doc != nil {
		return time.Duration(a.doc.Intervals.ReportS) * time.Second
	}
	return 60 * time.Second
}

func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func trimSpace(b []byte) []byte {
	start, end := 0, len(b)
	for start < end && (b[start] == ' ' || b[start] == '\n' || b[start] == '\r' || b[start] == '\t') {
		start++
	}
	for end > start && (b[end-1] == ' ' || b[end-1] == '\n' || b[end-1] == '\r' || b[end-1] == '\t') {
		end--
	}
	return b[start:end]
}
