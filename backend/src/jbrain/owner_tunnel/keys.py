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

import datetime as dt
import hashlib
from dataclasses import dataclass

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
# A certificate's notBefore is backdated so a phone whose clock runs a little slow accepts it.
_CLOCK_SKEW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class _Ca:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate


def spki_sha256(public_key: ec.EllipticCurvePublicKey) -> str:
    """Hex SHA-256 of the DER SubjectPublicKeyInfo: a device's identity. It is what Caddy's
    `tls.client.public_key_sha256` placeholder yields, and it survives cert re-issue."""
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()


def _same_key(a: ec.EllipticCurvePublicKey, b: object) -> bool:
    return isinstance(b, ec.EllipticCurvePublicKey) and spki_sha256(a) == spki_sha256(b)


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
