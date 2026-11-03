package pki

import (
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"
)

const node = "0b7f6a3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"

func TestCSRNamesTheNodeAndIsSelfSigned(t *testing.T) {
	key, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	csrPEM, err := CreateCSR(key, node)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(csrPEM)
	if block == nil || block.Type != "CERTIFICATE REQUEST" {
		t.Fatal("not a PEM CSR")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	if err := csr.CheckSignature(); err != nil {
		t.Fatal(err)
	}
	if csr.Subject.CommonName != node || len(csr.URIs) != 1 || csr.URIs[0].String() != NodeURIPrefix+node {
		t.Fatalf("csr names %q %v", csr.Subject.CommonName, csr.URIs)
	}
	pub, ok := csr.PublicKey.(*ecdsa.PublicKey)
	if !ok || pub.Curve.Params().Name != "P-256" || !pub.Equal(&key.PublicKey) {
		t.Fatal("csr does not carry the node's P-256 key")
	}
}

// testCA issues certificates with the same profile the panel uses (proxyhub/pki/issue.py).
type testCA struct {
	key  *ecdsa.PrivateKey
	cert *x509.Certificate
	pem  []byte
}

func newTestCA(t *testing.T) testCA {
	t.Helper()
	key, _ := GenerateKey()
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour * 365),
		IsCA:                  true,
		BasicConstraintsValid: true,
		MaxPathLenZero:        true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	return testCA{key: key, cert: cert, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

func (ca testCA) issue(t *testing.T, pub *ecdsa.PublicKey, nodeID string, eku x509.ExtKeyUsage) []byte {
	t.Helper()
	uri, _ := url.Parse(NodeURIPrefix + nodeID)
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(now.UnixNano()),
		Subject:      pkix.Name{CommonName: nodeID},
		NotBefore:    now.Add(-5 * time.Minute),
		NotAfter:     now.Add(30 * 24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{eku},
		URIs:         []*url.URL{uri},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.cert, pub, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func TestCheckIssued(t *testing.T) {
	ca, other := newTestCA(t), newTestCA(t)
	key, _ := GenerateKey()
	strange, _ := GenerateKey()
	now := time.Now()
	good := ca.issue(t, &key.PublicKey, node, x509.ExtKeyUsageClientAuth)
	if _, err := CheckIssued(good, ca.pem, ca.pem, key, node, now); err != nil {
		t.Fatalf("good certificate refused: %v", err)
	}
	cases := map[string]func() error{
		"other CA returned": func() error {
			_, err := CheckIssued(other.issue(t, &key.PublicKey, node, x509.ExtKeyUsageClientAuth), other.pem, ca.pem, key, node, now)
			return err
		},
		"signed by other CA": func() error {
			_, err := CheckIssued(other.issue(t, &key.PublicKey, node, x509.ExtKeyUsageClientAuth), ca.pem, ca.pem, key, node, now)
			return err
		},
		"someone else's key": func() error {
			_, err := CheckIssued(ca.issue(t, &strange.PublicKey, node, x509.ExtKeyUsageClientAuth), ca.pem, ca.pem, key, node, now)
			return err
		},
		"other node": func() error {
			_, err := CheckIssued(ca.issue(t, &key.PublicKey, "11111111-1111-1111-1111-111111111111", x509.ExtKeyUsageClientAuth), ca.pem, ca.pem, key, node, now)
			return err
		},
		"server auth only": func() error {
			_, err := CheckIssued(ca.issue(t, &key.PublicKey, node, x509.ExtKeyUsageServerAuth), ca.pem, ca.pem, key, node, now)
			return err
		},
		"expired": func() error {
			_, err := CheckIssued(good, ca.pem, ca.pem, key, node, now.Add(31*24*time.Hour))
			return err
		},
	}
	for name, run := range cases {
		if run() == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestIdentityRoundTripAndRenewPoint(t *testing.T) {
	ca := newTestCA(t)
	key, _ := GenerateKey()
	certPEM := ca.issue(t, &key.PublicKey, node, x509.ExtKeyUsageClientAuth)
	path := filepath.Join(t.TempDir(), "identity.pem")
	if err := SaveIdentity(path, key, certPEM); err != nil {
		t.Fatal(err)
	}
	st, _ := os.Stat(path)
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("identity mode = %v", st.Mode().Perm())
	}
	id, err := LoadIdentity(path)
	if err != nil {
		t.Fatal(err)
	}
	if !id.Key.Equal(key) || id.Cert.Subject.CommonName != node {
		t.Fatal("identity did not round-trip")
	}
	life := id.Cert.NotAfter.Sub(id.Cert.NotBefore)
	if got := RenewAt(id.Cert).Sub(id.Cert.NotBefore); got != life*2/3 {
		t.Fatalf("renew at %v into a %v lifetime", got, life)
	}
	if err := os.Chmod(path, 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadIdentity(path); err == nil {
		t.Fatal("group-readable identity accepted")
	}
}
