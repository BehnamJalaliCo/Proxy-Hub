// Command proxyhub-agent runs on every Proxy-Hub node and drives Telemt from the panel's desired
// state (docs/10 section 3.5, docs/11).
//
//	proxyhub-agent [-config FILE] run            register if needed, then loop (systemd)
//	proxyhub-agent [-config FILE] once           one full cycle, then exit
//	proxyhub-agent [-config FILE] register       one-time registration only
//	proxyhub-agent check-config TELEMT_TOML      refuse a Telemt config that breaks an invariant
//	proxyhub-agent version
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/agent"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/telemt"
)

// version is set at build time: -ldflags "-X main.version=<version>".
var version = "0.1.0"

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	fs := flag.NewFlagSet("proxyhub-agent", flag.ContinueOnError)
	configPath := fs.String("config", "/etc/proxyhub-agent/agent.json", "agent config file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	cmd := "run"
	if fs.NArg() > 0 {
		cmd = fs.Arg(0)
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	switch cmd {
	case "version":
		fmt.Println(version)
		return 0
	case "check-config":
		if fs.NArg() != 2 {
			fmt.Fprintln(os.Stderr, "usage: proxyhub-agent check-config TELEMT_TOML")
			return 2
		}
		data, err := os.ReadFile(fs.Arg(1))
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		if err := telemt.CheckInvariants(data); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		fmt.Println("ok")
		return 0
	case "run", "once", "register":
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", cmd)
		return 2
	}

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Error("config", "error", err.Error())
		return 1
	}
	a, err := agent.New(cfg, version, log)
	if err != nil {
		log.Error("start", "error", err.Error())
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	switch cmd {
	case "register":
		if a.Registered() {
			log.Info("already registered")
			return 0
		}
		if err := a.Register(ctx); err != nil {
			log.Error("register", "error", err.Error())
			return 1
		}
	case "once":
		if err := a.Once(ctx); err != nil {
			log.Error("cycle", "error", err.Error())
			return 1
		}
	case "run":
		if err := a.Run(ctx); err != nil && ctx.Err() == nil {
			log.Error("run", "error", err.Error())
			return 1
		}
	}
	return 0
}
