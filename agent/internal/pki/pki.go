// Package pki creates the node's own key and CSR, and checks certificates the panel returns.
//
// The EC P-256 private key is generated here and never leaves the node: only the CSR (public key
// plus the node id) is sent. The key and the current certificate are kept together in one 0600
// PEM file, replaced atomically, so they can never get out of step.
package pki

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"net/url"
	"os"
	"time"

	"github.com/BehnamJalaliCo/Proxy-Hub/agent/internal/fsutil"
)

// NodeURIPrefix is the SAN URI scheme the panel puts in every node certificate.
const NodeURIPrefix = "urn:proxyhub:node:"

// GenerateKey creates a fresh EC P-256 key.
func GenerateKey() (*ecdsa.PrivateKey, error) {
	return ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
}

// CreateCSR builds a PEM CSR for the node: CN = node id, SAN URI urn:proxyhub:node:<id>.
func CreateCSR(key *ecdsa.PrivateKey, nodeID string) ([]byte, error) {
	uri, err := url.Parse(NodeURIPrefix + nodeID)
	if err != nil {
		return nil, fmt.Errorf("node uri: %w", err)
	}
	tmpl := &x509.CertificateRequest{
		Subject:            pkix.Name{CommonName: nodeID},
		URIs:               []*url.URL{uri},
		SignatureAlgorithm: x509.ECDSAWithSHA256,
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, tmpl, key)
	if err != nil {
		return nil, fmt.Errorf("create csr: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}

// EncodeKey returns the key as a PKCS#8 PEM block.
func EncodeKey(key *ecdsa.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, fmt.Errorf("marshal key: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

// ParseCertificatePEM parses exactly one PEM certificate.
func ParseCertificatePEM(data []byte) (*x509.Certificate, error) {
	block, rest := pem.Decode(data)
	if block == nil || block.Type != "CERTIFICATE" || len(bytes.TrimSpace(rest)) != 0 {
		return nil, errors.New("expected exactly one PEM certificate")
	}
	return x509.ParseCertificate(block.Bytes)
}

// CheckIssued verifies a certificate the panel issued for us: the CA it returned is the CA we were
// provisioned with (byte-identical), the certificate chains to it for client auth, carries our
// public key, and names exactly our node.
func CheckIssued(certPEM, returnedCAPEM, pinnedCAPEM []byte, key *ecdsa.PrivateKey, nodeID string, now time.Time) (*x509.Certificate, error) {
	pinned, err := ParseCertificatePEM(pinnedCAPEM)
	if err != nil {
		return nil, fmt.Errorf("pinned ca: %w", err)
	}
	returned, err := ParseCertificatePEM(returnedCAPEM)
	if err != nil {
		return nil, fmt.Errorf("returned ca: %w", err)
	}
	if !bytes.Equal(pinned.Raw, returned.Raw) {
		return nil, errors.New("panel returned a different CA than the one this node was provisioned with")
	}
	cert, err := ParseCertificatePEM(certPEM)
	if err != nil {
		return nil, fmt.Errorf("issued certificate: %w", err)
	}
	pub, ok := cert.PublicKey.(*ecdsa.PublicKey)
	if !ok || !pub.Equal(&key.PublicKey) {
		return nil, errors.New("issued certificate does not carry this node's key")
	}
	roots := x509.NewCertPool()
	roots.AddCert(pinned)
	if _, err := cert.Verify(x509.VerifyOptions{
		Roots:       roots,
		CurrentTime: now,
		KeyUsages:   []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}); err != nil {
		return nil, fmt.Errorf("issued certificate does not verify: %w", err)
	}
	if cert.Subject.CommonName != nodeID || len(cert.URIs) != 1 || cert.URIs[0].String() != NodeURIPrefix+nodeID {
		return nil, errors.New("issued certificate names another node")
	}
	return cert, nil
}

// RenewAt is when a certificate should be replaced: two thirds of the way through its validity.
func RenewAt(cert *x509.Certificate) time.Time {
	life := cert.NotAfter.Sub(cert.NotBefore)
	return cert.NotBefore.Add(life * 2 / 3)
}

// Identity is the node's key and current certificate.
type Identity struct {
	Key  *ecdsa.PrivateKey
	Cert *x509.Certificate
	TLS  tls.Certificate
}

// SaveIdentity writes key + certificate to one 0600 file atomically.
func SaveIdentity(path string, key *ecdsa.PrivateKey, certPEM []byte) error {
	keyPEM, err := EncodeKey(key)
	if err != nil {
		return err
	}
	data := append(append([]byte{}, certPEM...), keyPEM...)
	return fsutil.WriteAtomic(path, data, 0o600, fsutil.KeepOwner)
}

// LoadIdentity reads the identity file; it refuses a file readable by group or others.
func LoadIdentity(path string) (*Identity, error) {
	st, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if st.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("identity file %s must be 0600", path)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	pair, err := tls.X509KeyPair(data, data)
	if err != nil {
		return nil, fmt.Errorf("identity file: %w", err)
	}
	key, ok := pair.PrivateKey.(*ecdsa.PrivateKey)
	if !ok {
		return nil, errors.New("identity key is not ECDSA")
	}
	cert, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return nil, err
	}
	pair.Leaf = cert
	return &Identity{Key: key, Cert: cert, TLS: pair}, nil
}
