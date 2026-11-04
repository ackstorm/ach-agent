"""The sandbox service path trusts ACH_AGENT_EXECUTION_CA_FILE, and only it."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ach_runtime.harness.control import execution_verify

_USAGES = ("digital_signature", "content_commitment", "key_encipherment", "data_encipherment",
           "key_agreement", "key_cert_sign", "crl_sign", "encipher_only", "decipher_only")


def _key_usage(*on: str) -> x509.KeyUsage:
    return x509.KeyUsage(**{name: name in on for name in _USAGES})


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _window(builder: x509.CertificateBuilder) -> x509.CertificateBuilder:
    now = datetime.datetime.now(datetime.timezone.utc)
    return builder.not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(hours=1))


def _ca(tmp: Path, cn: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (_window(x509.CertificateBuilder()).subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(_key_usage("key_cert_sign", "crl_sign"), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    path = tmp / f"{cn}.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert, path


def _server_context(tmp: Path, ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (_window(x509.CertificateBuilder()).subject_name(_name("openshell-server")).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(_key_usage("digital_signature"), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    crt, pem = tmp / "server.crt", tmp / "server.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(crt, pem)
    return ctx


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n")
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    await writer.drain()
    writer.close()


async def test_execution_client_trusts_the_configured_ca_only(tmp_path: Path) -> None:
    ca_key, ca_cert, ca_file = _ca(tmp_path, "gateway-ca")
    _, _, other_file = _ca(tmp_path, "other-ca")
    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=_server_context(tmp_path, ca_key, ca_cert))
    url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}/healthz"
    try:
        async with httpx.AsyncClient(verify=execution_verify({"ACH_AGENT_EXECUTION_CA_FILE": str(ca_file)})) as client:
            assert (await client.get(url)).text == "ok"
        async with httpx.AsyncClient(verify=execution_verify({"ACH_AGENT_EXECUTION_CA_FILE": str(other_file)})) as client:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await client.get(url)
    finally:
        server.close()
        await server.wait_closed()


def test_execution_client_keeps_the_default_trust_without_a_ca_file() -> None:
    assert execution_verify({}) is True
