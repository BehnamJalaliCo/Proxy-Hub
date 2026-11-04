// Command proxyhub-csr runs the agent's real key, CSR and certificate-check code on files. The
// panel's tests use it so CSR signing, and the node's acceptance of what the panel issues, are
// exercised with exactly the code a node runs.
//
//	proxyhub-csr -node-id UUID -key-out FILE -csr-out FILE
//	proxyhub-csr -verify -node-id UUID -key FILE -cert FILE -ca FILE [-returned-ca FILE]
package main

import (
	"crypto/ecdsa"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/pki"
)

func main() {
	nodeID := flag.String("node-id", "", "node id (CN and SAN URI)")
	keyOut := flag.String("key-out", "", "private key output file (written 0600)")
	csrOut := flag.String("csr-out", "", "CSR output file")
	verify := flag.Bool("verify", false, "check an issued certificate the way the agent does")
	keyIn := flag.String("key", "", "verify: the node key (PEM)")
	certIn := flag.String("cert", "", "verify: the issued certificate (PEM)")
	caIn := flag.String("ca", "", "verify: the CA the node was provisioned with (PEM)")
	returnedCA := flag.String("returned-ca", "", "verify: the CA the panel returned (default: -ca)")
	flag.Parse()
	var err error
	switch {
	case *nodeID == "":
		flag.Usage()
		os.Exit(2)
	case *verify:
		err = check(*nodeID, *keyIn, *certIn, *caIn, *returnedCA)
	case *keyOut != "" && *csrOut != "":
		err = write(*nodeID, *keyOut, *csrOut)
	default:
		flag.Usage()
		os.Exit(2)
	}
	if err != nil {
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

func check(nodeID, keyPath, certPath, caPath, returnedPath string) error {
	if returnedPath == "" {
		returnedPath = caPath
	}
	keyPEM, err := os.ReadFile(keyPath)
	if err != nil {
		return err
	}
	block, _ := pem.Decode(keyPEM)
	if block == nil {
		return errors.New("no PEM key")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return err
	}
	key, ok := parsed.(*ecdsa.PrivateKey)
	if !ok {
		return errors.New("not an ECDSA key")
	}
	certPEM, err := os.ReadFile(certPath)
	if err != nil {
		return err
	}
	pinned, err := os.ReadFile(caPath)
	if err != nil {
		return err
	}
	returned, err := os.ReadFile(returnedPath)
	if err != nil {
		return err
	}
	if _, err := pki.CheckIssued(certPEM, returned, pinned, key, nodeID, time.Now()); err != nil {
		return err
	}
	fmt.Println("ok")
	return nil
}
