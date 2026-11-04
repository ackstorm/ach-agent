// SPDX-License-Identifier: MIT

package openshell

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

// operatorTLSSecret is what cert-manager writes for the ach-agent-operator Certificate: a CA, and a
// client certificate with OU=operator signed by it.
func operatorTLSSecret(t *testing.T) *corev1.Secret {
	t.Helper()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	caTmpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "openshell-ca"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	leafTmpl := &x509.Certificate{SerialNumber: big.NewInt(2),
		Subject:   pkix.Name{CommonName: "ach-agent-operator", OrganizationalUnit: []string{"operator"}},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTmpl, caCert, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: "openshell", Name: "ach-agent-operator-tls"},
		Type:       corev1.SecretTypeTLS,
		Data: map[string][]byte{
			"ca.crt":  pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}),
			"tls.crt": pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER}),
			"tls.key": pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}),
		},
	}
}

func TestOpenShellFromEnv_TLSSecretGivesTheClientItsCertificate(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	t.Setenv("TMPDIR", tmp)
	secret := operatorTLSSecret(t)
	reader := fake.NewClientBuilder().WithObjects(secret).Build()
	t.Setenv("ACH_AGENT_OPENSHELL_GATEWAY", "https://openshell.openshell.svc.cluster.local:8080")
	t.Setenv("ACH_AGENT_OPENSHELL_TLS_SECRET", "openshell/ach-agent-operator-tls")
	t.Setenv("ACH_AGENT_OPENSHELL_SERVICE_URL", "https://{workspace}--{sandbox}.{namespace}.svc.cluster.local:8080")
	o, err := OpenShellFromEnv(ctx, reader)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(o.Config.CA, secret.Data["ca.crt"]) {
		t.Errorf("Config.CA = %q, want the Secret's ca.crt", o.Config.CA)
	}
	keys, err := filepath.Glob(filepath.Join(tmp, "openshell-tls-*", "tls.key"))
	if err != nil || len(keys) != 1 {
		t.Fatalf("tls.key files under TMPDIR = %v (%v), want one", keys, err)
	}
	if fi, err := os.Stat(keys[0]); err != nil || fi.Mode().Perm() != 0o600 {
		t.Errorf("tls.key mode = %v (%v), want 0600", fi.Mode().Perm(), err)
	}
	if fi, err := os.Stat(filepath.Dir(keys[0])); err != nil || fi.Mode().Perm() != 0o700 {
		t.Errorf("TLS dir mode = %v (%v), want 0700", fi.Mode().Perm(), err)
	}

	t.Setenv("ACH_AGENT_OPENSHELL_GATEWAY", "http://openshell.openshell.svc.cluster.local:8080")
	if _, err := OpenShellFromEnv(ctx, reader); err == nil {
		t.Error("a plaintext gateway address with a TLS Secret must fail the start")
	}
}

func TestOpenShellFromEnv_AMissingOrIncompleteTLSSecretFailsTheStart(t *testing.T) {
	ctx := context.Background()
	t.Setenv("TMPDIR", t.TempDir())
	t.Setenv("ACH_AGENT_OPENSHELL_GATEWAY", "https://openshell.openshell.svc.cluster.local:8080")
	incomplete := operatorTLSSecret(t)
	delete(incomplete.Data, "tls.key")
	reader := fake.NewClientBuilder().WithObjects(incomplete).Build()
	for ref, why := range map[string]string{
		"openshell/missing":                "a missing Secret",
		"openshell/ach-agent-operator-tls": "a Secret without tls.key",
		"ach-agent-operator-tls":           "a reference without a namespace",
	} {
		t.Setenv("ACH_AGENT_OPENSHELL_TLS_SECRET", ref)
		if _, err := OpenShellFromEnv(ctx, reader); err == nil {
			t.Errorf("%s (%s) must fail the start", why, ref)
		}
	}
}

func TestOpenShellFromEnv_WithoutTLSSecretTheGatewayIsPlaintext(t *testing.T) {
	t.Setenv("ACH_AGENT_OPENSHELL_GATEWAY", "http://openshell.openshell.svc.cluster.local:8080")
	t.Setenv("ACH_AGENT_OPENSHELL_TLS_SECRET", "")
	t.Setenv("ACH_AGENT_OPENSHELL_SERVICE_URL", "")
	o, err := OpenShellFromEnv(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if o.Config.CA != nil {
		t.Errorf("Config.CA = %q, want nil without a TLS Secret", o.Config.CA)
	}
	if want := "http://{workspace}--{sandbox}.{namespace}.svc.cluster.local:8080"; o.Config.ServiceURL != want {
		t.Errorf("ServiceURL = %q, want %q", o.Config.ServiceURL, want)
	}
}
