package agent

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/config"
)

// docs/11 E6: Telemt's REST API can return client IP addresses, so the agent must never call it.
// The only Telemt endpoint the agent requests is /metrics (aggregate counters), and the rendered
// config keeps the API off (telemt.CheckInvariants). This test reads the agent's own non-test
// source, so a new HTTP call or a Telemt API path anywhere in the agent fails here first.
func TestTelemtIsOnlyAskedForMetrics(t *testing.T) {
	root := filepath.Join("..", "..") // the agent module
	fset := token.NewFileSet()
	requestSites := map[string]int{}
	scrapeCalls := 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == "testdata" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		rel = filepath.ToSlash(rel)
		ast.Inspect(file, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.BasicLit:
				if n.Kind != token.STRING {
					return true
				}
				value, err := strconv.Unquote(n.Value)
				if err != nil {
					t.Errorf("%s: unreadable string literal", fset.Position(n.Pos()))
					return true
				}
				// Telemt's API lives under /v1/; the only /v1/ paths the agent knows are the panel's.
				if strings.Contains(value, "/v1/") && !strings.HasPrefix(value, "/agent/v1/") {
					t.Errorf("%s: %q looks like a Telemt API path", fset.Position(n.Pos()), value)
				}
			case *ast.SelectorExpr:
				if pkg, ok := n.X.(*ast.Ident); ok && pkg.Name == "http" {
					switch n.Sel.Name {
					case "NewRequest", "NewRequestWithContext", "Get", "Head", "Post", "PostForm":
						requestSites[rel]++
					}
				}
			case *ast.CallExpr:
				sel, ok := n.Fun.(*ast.SelectorExpr)
				if !ok || sel.Sel.Name != "Scrape" {
					return true
				}
				if pkg, ok := sel.X.(*ast.Ident); !ok || pkg.Name != "metrics" {
					return true
				}
				scrapeCalls++
				urlArg, ok := n.Args[len(n.Args)-1].(*ast.CallExpr)
				if !ok {
					t.Errorf("%s: metrics.Scrape must be given cfg.MetricsURL()", fset.Position(n.Pos()))
					return true
				}
				if fn, ok := urlArg.Fun.(*ast.SelectorExpr); !ok || fn.Sel.Name != "MetricsURL" {
					t.Errorf("%s: metrics.Scrape must be given cfg.MetricsURL()", fset.Position(n.Pos()))
				}
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	// One request site towards Telemt (the scrape) and two towards the panel (GET desired, POSTs).
	want := map[string]int{"internal/metrics/metrics.go": 1, "internal/panel/client.go": 2}
	if !reflect.DeepEqual(requestSites, want) {
		t.Fatalf("HTTP requests are built in %v, want %v: review any new one against docs/11 E6", requestSites, want)
	}
	if scrapeCalls != 1 {
		t.Fatalf("metrics.Scrape is called %d times outside tests, want 1", scrapeCalls)
	}
	cfg := config.Config{MetricsListen: "127.0.0.1:9090"}
	if got := cfg.MetricsURL(); got != "http://127.0.0.1:9090/metrics" {
		t.Fatalf("MetricsURL() = %q", got)
	}
}
