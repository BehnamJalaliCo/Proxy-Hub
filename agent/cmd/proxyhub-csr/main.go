// Command proxyhub-csr runs the agent's real key and CSR code once and writes the results to files.
// The panel's tests use it so CSR signing is exercised with exactly what a node sends.
//
//	proxyhub-csr -node-id UUID -key-out FILE -csr-out FILE
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/pki"
)

func main() {
	nodeID := flag.String("node-id", "", "node id (CN and SAN URI)")
	keyOut := flag.String("key-out", "", "private key output file (written 0600)")
	csrOut := flag.String("csr-out", "", "CSR output file")
	flag.Parse()
	if *nodeID == "" || *keyOut == "" || *csrOut == "" {
		flag.Usage()
		os.Exit(2)
	}
	if err := write(*nodeID, *keyOut, *csrOut); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func write(nodeID, keyOut, csrOut string) error {
	key, err := pki.GenerateKey()
	if err != nil {
		return err
	}
	csr, err := pki.CreateCSR(key, nodeID)
	if err != nil {
		return err
	}
	keyPEM, err := pki.EncodeKey(key)
	if err != nil {
		return err
	}
	if err := fsutil.WriteAtomic(keyOut, keyPEM, 0o600, fsutil.KeepOwner); err != nil {
		return err
	}
	return fsutil.WriteAtomic(csrOut, csr, 0o644, fsutil.KeepOwner)
}
