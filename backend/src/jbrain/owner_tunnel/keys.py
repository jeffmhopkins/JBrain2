"""The box's owner-tunnel key material (docs/plans/OWNER_TUNNEL_PLAN.md §3.1).

Two CAs, both made here and never anywhere else:

- the **owner CA** signs the owner site's TLS leaf. The owner app trusts it, and only it, for
  `owner.jbrain.home.arpa`, so it is long-lived: replacing it means re-releasing the app;
- the **enrollment CA** signs the phones' client certificates. Caddy accepts only it on the
  owner site.

`ensure` is idempotent and runs on every update (`python -m jbrain.cli owner-keys ensure`), so
the leaf renews itself with no terminal step. It keeps whatever is valid and replaces only what
is missing, mismatched or due.
"""

import base64
import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import cast

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from jbrain.storage import OwnerKeyFiles

OWNER_HOST = "owner.jbrain.home.arpa"

CA_LIFETIME = dt.timedelta(days=20 * 365)
LEAF_LIFETIME = dt.timedelta(days=365)
# Updates are not daily, so a leaf is renewed well before it lapses.
LEAF_RENEW_BEFORE = dt.timedelta(days=60)
# A phone's certificate outlives the phone. Revocation is the api's job (§3.4), not expiry's,
# and a phone must never be locked out because nobody renewed it.
DEVICE_LIFETIME = dt.timedelta(days=10 * 365)
# A certificate's notBefore is backdated so a phone whose clock runs a little slow accepts it.
_CLOCK_SKEW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class _Ca:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate


def public_key_sha256(public_key: ec.EllipticCurvePublicKey) -> str:
    """A device's identity: hex SHA-256 of the uncompressed EC point. That is exactly what
    Caddy's `tls.client.public_key_sha256` placeholder yields for an EC client key (it hashes
    the raw point, not the DER SubjectPublicKeyInfo), so the owner site's `X-JBrain-Device`
    header matches this. It survives certificate re-issue."""
    point = public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return hashlib.sha256(point).hexdigest()


def spki_pin(cert: x509.Certificate) -> str:
    """The certificate's key as an Android `<pin digest="SHA-256">` value: base64 of the
    SHA-256 of its DER SubjectPublicKeyInfo. The enrollment QR carries the owner CA's and the
    LAN root's, so the app trusts exactly those keys from its first request."""
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def _same_key(a: ec.EllipticCurvePublicKey, b: object) -> bool:
    return isinstance(b, ec.EllipticCurvePublicKey) and public_key_sha256(a) == public_key_sha256(b)


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _load_key(data: bytes | None) -> ec.EllipticCurvePrivateKey | None:
    if data is None:
        return None
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except ValueError:
        return None
    return key if isinstance(key, ec.EllipticCurvePrivateKey) else None


def _load_certs(data: bytes | None) -> list[x509.Certificate]:
    if data is None:
        return []
    try:
        return x509.load_pem_x509_certificates(data)
    except ValueError:
        return []


def _load_ca(files: OwnerKeyFiles, stem: str, now: dt.datetime) -> _Ca | None:
    key = _load_key(files.read(f"{stem}.key"))
    certs = _load_certs(files.read(f"{stem}.crt"))
    if key is None or len(certs) != 1:
        return None
    cert = certs[0]
    if not _same_key(key.public_key(), cert.public_key()) or cert.not_valid_after_utc <= now:
        return None
    return _Ca(key, cert)


def _make_ca(common_name: str, now: dt.datetime, permitted_dns: str | None) -> _Ca:
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + CA_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if permitted_dns is not None:
        # Even if this CA's key ever leaked, a certificate it signs is good for the owner
        # host and nothing else.
        builder = builder.add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(permitted_dns)], excluded_subtrees=None
            ),
            critical=True,
        )
    return _Ca(key, builder.sign(key, hashes.SHA256()))


def _ensure_ca(
    files: OwnerKeyFiles, stem: str, common_name: str, now: dt.datetime, permitted_dns: str | None
) -> tuple[_Ca, bool]:
    ca = _load_ca(files, stem, now)
    if ca is not None:
        return ca, False
    ca = _make_ca(common_name, now, permitted_dns)
    # Key first: a crash before the cert lands leaves a mismatched pair, which the next run
    # replaces whole rather than trusting a cert whose key is gone.
    files.write(f"{stem}.key", _key_pem(ca.key))
    files.write(f"{stem}.crt", _cert_pem(ca.cert))
    return ca, True


def _leaf_is_current(files: OwnerKeyFiles, owner_ca: _Ca, now: dt.datetime) -> bool:
    key = _load_key(files.read("site.key"))
    chain = _load_certs(files.read("site.crt"))
    if key is None or not chain:
        return False
    leaf = chain[0]
    return (
        _same_key(key.public_key(), leaf.public_key())
        and leaf.issuer == owner_ca.cert.subject
        and _signed_by(leaf, owner_ca.cert)
        and leaf.not_valid_after_utc - now > LEAF_RENEW_BEFORE
    )


def _signed_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def _issue_leaf(owner_ca: _Ca, now: dt.datetime) -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    key = _new_key()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, OWNER_HOST)]))
        .issuer_name(owner_ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + LEAF_LIFETIME)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(OWNER_HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(owner_ca.key.public_key()),
            critical=False,
        )
        .sign(owner_ca.key, hashes.SHA256())
    )
    return key, _cert_pem(leaf) + _cert_pem(owner_ca.cert)


def ensure(files: OwnerKeyFiles, now: dt.datetime | None = None) -> list[str]:
    """Make whatever is missing or due; return what was made, for the update log."""
    now = now or dt.datetime.now(dt.UTC)
    made: list[str] = []
    owner_ca, new = _ensure_ca(files, "owner-ca", "JBrain owner CA", now, OWNER_HOST)
    if new:
        made.append("owner CA")
    _, new = _ensure_ca(files, "enroll-ca", "JBrain enrollment CA", now, None)
    if new:
        made.append("enrollment CA")
    if not _leaf_is_current(files, owner_ca, now):
        key, chain = _issue_leaf(owner_ca, now)
        files.write("site.key", _key_pem(key))
        files.write("site.crt", chain)
        made.append("owner site certificate")
    return made


class KeysNotReady(Exception):
    """The CAs have not been made yet (the update's `owner-keys ensure` has not run)."""


@dataclass(frozen=True)
class IssuedDevice:
    key_hash: str
    public_key_der: bytes
    cert_pem: bytes
    owner_ca_pem: bytes
    enroll_ca_pem: bytes


def parse_device_csr(csr_pem: bytes) -> x509.CertificateSigningRequest:
    """A phone's certificate request, or ValueError. Its self-signature proves the phone holds
    the key, and the key must be P-256: that is what the phone's secure hardware makes, and
    `public_key_sha256` is defined on it."""
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("the request's signature does not verify")
    key = csr.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("the request's key is not P-256")
    return csr


def owner_ca_cert(files: OwnerKeyFiles, now: dt.datetime | None = None) -> x509.Certificate:
    ca = _load_ca(files, "owner-ca", now or dt.datetime.now(dt.UTC))
    if ca is None:
        raise KeysNotReady("owner CA")
    return ca.cert


def issue_device(
    files: OwnerKeyFiles, csr: x509.CertificateSigningRequest, now: dt.datetime | None = None
) -> IssuedDevice:
    """Sign a phone's client certificate with the enrollment CA. Only the key is taken from the
    request: the subject and extensions are the box's own, so nothing the phone asks for ends
    up in what Caddy trusts."""
    now = now or dt.datetime.now(dt.UTC)
    enroll_ca = _load_ca(files, "enroll-ca", now)
    owner_ca = _load_ca(files, "owner-ca", now)
    if enroll_ca is None or owner_ca is None:
        raise KeysNotReady("enrollment CA" if enroll_ca is None else "owner CA")
    key = cast(ec.EllipticCurvePublicKey, csr.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JBrain owner phone")]))
        .issuer_name(enroll_ca.cert.subject)
        .public_key(key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(min(now + DEVICE_LIFETIME, enroll_ca.cert.not_valid_after_utc))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(enroll_ca.key.public_key()),
            critical=False,
        )
        .sign(enroll_ca.key, hashes.SHA256())
    )
    return IssuedDevice(
        key_hash=public_key_sha256(key),
        public_key_der=key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
        cert_pem=_cert_pem(cert),
        owner_ca_pem=_cert_pem(owner_ca.cert),
        enroll_ca_pem=_cert_pem(enroll_ca.cert),
    )
