"""The owner tunnel's key material (jbrain/owner_tunnel/keys.py) and its file store.

Security path: these certificates are what the owner app and Caddy trust, so every branch of
`ensure` is covered, including the ones that throw a bad file away.
"""

import contextlib
import datetime as dt
import ssl
import stat
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from jbrain import cli
from jbrain.owner_tunnel import keys
from jbrain.storage import FsOwnerKeyFiles

NOW = dt.datetime(2026, 10, 17, tzinfo=dt.UTC)


@pytest.fixture
def files(tmp_path: Path) -> FsOwnerKeyFiles:
    return FsOwnerKeyFiles(tmp_path / "private", tmp_path / "shared")


def _cert(files: FsOwnerKeyFiles, name: str) -> x509.Certificate:
    data = files.read(name)
    assert data is not None
    return x509.load_pem_x509_certificates(data)[0]


def test_first_run_makes_everything(files: FsOwnerKeyFiles) -> None:
    assert keys.ensure(files, NOW) == ["owner CA", "enrollment CA", "owner site certificate"]
    for name in ("owner-ca.key", "owner-ca.crt", "enroll-ca.key", "enroll-ca.crt"):
        assert files.read(name) is not None


def test_second_run_changes_nothing(files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    before = {n: files.read(n) for n in ("owner-ca.crt", "enroll-ca.crt", "site.crt")}
    assert keys.ensure(files, NOW + dt.timedelta(days=30)) == []
    assert {n: files.read(n) for n in before} == before


def test_ca_keys_stay_out_of_the_proxy_volume(tmp_path: Path, files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    shared = sorted(p.name for p in (tmp_path / "shared").iterdir())
    assert shared == ["enroll-ca.crt", "owner-ca.crt", "site.crt", "site.key"]
    private = sorted(p.name for p in (tmp_path / "private").iterdir())
    assert private == ["enroll-ca.key", "owner-ca.key"]


def test_keys_are_private_and_certs_readable(tmp_path: Path, files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    assert stat.S_IMODE((tmp_path / "shared" / "site.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "private" / "owner-ca.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "shared" / "site.crt").stat().st_mode) == 0o644


def test_owner_ca_is_constrained_to_the_owner_host(files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    ca = _cert(files, "owner-ca.crt")
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    assert constraints.value.permitted_subtrees == [x509.DNSName(keys.OWNER_HOST)]
    basic = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca and basic.path_length == 0
    assert ca.not_valid_after_utc - NOW > dt.timedelta(days=19 * 365)


def test_enrollment_ca_is_unconstrained_and_separate(files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    enroll = _cert(files, "enroll-ca.crt")
    with pytest.raises(x509.ExtensionNotFound):
        enroll.extensions.get_extension_for_class(x509.NameConstraints)
    assert enroll.subject != _cert(files, "owner-ca.crt").subject


def test_site_chain_verifies_against_the_owner_ca_alone(
    tmp_path: Path, files: FsOwnerKeyFiles
) -> None:
    keys.ensure(files)
    chain = x509.load_pem_x509_certificates(files.read("site.crt") or b"")
    leaf = chain[0]
    assert chain[1] == _cert(files, "owner-ca.crt")
    leaf.verify_directly_issued_by(chain[1])
    assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value == (
        x509.SubjectAlternativeName([x509.DNSName(keys.OWNER_HOST)])
    )
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    # A real TLS stack takes the files as they are: what Caddy loads, and what the phone
    # validates with the owner CA as its only anchor.
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(tmp_path / "shared" / "site.crt", tmp_path / "shared" / "site.key")
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.load_verify_locations(cadata=(files.read("owner-ca.crt") or b"").decode())
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    c = client.wrap_bio(client_in, client_out, server_hostname=keys.OWNER_HOST)
    s = server.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(10):
        for side in (c, s):
            with contextlib.suppress(ssl.SSLWantReadError):
                side.do_handshake()
        server_in.write(client_out.read())
        client_in.write(server_out.read())
    assert c.version() == "TLSv1.3"
    peer = c.getpeercert()
    assert peer is not None
    assert peer["subjectAltName"] == (("DNS", keys.OWNER_HOST),)


def test_leaf_renews_when_due_and_cas_stay(files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    owner_ca = files.read("owner-ca.crt")
    old_leaf = files.read("site.crt")
    due = NOW + keys.LEAF_LIFETIME - keys.LEAF_RENEW_BEFORE + dt.timedelta(days=1)
    assert keys.ensure(files, due) == ["owner site certificate"]
    assert files.read("owner-ca.crt") == owner_ca
    assert files.read("site.crt") != old_leaf
    assert _cert(files, "site.crt").not_valid_after_utc > due + dt.timedelta(days=300)


def test_a_new_owner_ca_reissues_the_leaf(files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    files.write("owner-ca.key", b"garbage")
    assert keys.ensure(files, NOW) == ["owner CA", "owner site certificate"]
    _cert(files, "site.crt").verify_directly_issued_by(_cert(files, "owner-ca.crt"))


def test_ca_cert_without_its_key_is_replaced(files: FsOwnerKeyFiles) -> None:
    """A crash between the key and the cert write, or a key from another CA."""
    keys.ensure(files, NOW)
    other = FsOwnerKeyFiles(files._private.parent / "o1", files._shared.parent / "o2")  # noqa: SLF001
    keys.ensure(other, NOW)
    files.write("enroll-ca.key", other.read("enroll-ca.key") or b"")
    assert keys.ensure(files, NOW) == ["enrollment CA"]
    enroll = _cert(files, "enroll-ca.crt")
    key = serialization.load_pem_private_key(files.read("enroll-ca.key") or b"", None)
    assert keys.public_key_sha256(enroll.public_key()) == keys.public_key_sha256(key.public_key())  # type: ignore[arg-type]


def test_expired_ca_is_replaced(files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    later = NOW + keys.CA_LIFETIME + dt.timedelta(days=1)
    assert keys.ensure(files, later) == ["owner CA", "enrollment CA", "owner site certificate"]


@pytest.mark.parametrize("bad", [b"not pem", None])
def test_unreadable_leaf_is_reissued(files: FsOwnerKeyFiles, bad: bytes | None) -> None:
    keys.ensure(files, NOW)
    if bad is None:
        (files._shared / "site.crt").unlink()  # noqa: SLF001
    else:
        files.write("site.crt", bad)
    assert keys.ensure(files, NOW) == ["owner site certificate"]


def test_leaf_key_mismatch_is_reissued(files: FsOwnerKeyFiles) -> None:
    keys.ensure(files, NOW)
    files.write("site.key", files.read("owner-ca.key") or b"")
    assert keys.ensure(files, NOW) == ["owner site certificate"]


def test_leaf_from_a_lookalike_ca_is_reissued(files: FsOwnerKeyFiles) -> None:
    """Same issuer name, different key: the signature check, not the name, decides."""
    keys.ensure(files, NOW)
    other = FsOwnerKeyFiles(files._private.parent / "o1", files._shared.parent / "o2")  # noqa: SLF001
    keys.ensure(other, NOW)
    files.write("site.key", other.read("site.key") or b"")
    files.write("site.crt", other.read("site.crt") or b"")
    assert keys.ensure(files, NOW) == ["owner site certificate"]


def test_non_ec_key_is_not_accepted(files: FsOwnerKeyFiles) -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    keys.ensure(files, NOW)
    ed = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    files.write("enroll-ca.key", ed)
    assert keys.ensure(files, NOW) == ["enrollment CA"]


def test_store_refuses_foreign_names(files: FsOwnerKeyFiles) -> None:
    with pytest.raises(ValueError):
        files.read("../etc/passwd")
    with pytest.raises(ValueError):
        files.write("owner-ca.pem", b"x")


def test_store_write_replaces_whole(tmp_path: Path, files: FsOwnerKeyFiles) -> None:
    files.write("site.crt", b"one")
    files.write("site.crt", b"two")
    assert files.read("site.crt") == b"two"
    assert [p.name for p in (tmp_path / "shared").iterdir()] == ["site.crt"]


def test_cli_ensure_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JBRAIN_OWNER_KEYS_DIR", str(tmp_path / "k"))
    monkeypatch.setenv("JBRAIN_OWNER_SITE_DIR", str(tmp_path / "s"))
    assert cli.main(["owner-keys", "ensure"]) == 0
    assert "made: owner CA, enrollment CA, owner site certificate" in capsys.readouterr().out
    assert (tmp_path / "k" / "owner-ca.key").exists()
    assert cli.main(["owner-keys", "ensure"]) == 0
    assert "all current" in capsys.readouterr().out


# --- deploy wiring -------------------------------------------------------------------

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"


def _volume_users(volume: str) -> dict[str, list[str]]:
    compose = yaml.safe_load((_DEPLOY / "docker-compose.yml").read_text())
    assert volume in compose["volumes"]
    users = {
        name: [m for m in spec.get("volumes") or [] if str(m).startswith(f"{volume}:")]
        for name, spec in compose["services"].items()
    }
    return {name: mounts for name, mounts in users.items() if mounts}


def test_ca_keys_volume_is_the_apis_alone() -> None:
    assert _volume_users("owner_keys") == {"api": ["owner_keys:/run/jbrain-owner-keys"]}


def test_site_volume_is_written_only_by_the_api() -> None:
    users = _volume_users("owner_site")
    assert users["api"] == ["owner_site:/run/jbrain-owner-site"]
    assert all(m.endswith(":ro") for name, ms in users.items() if name != "api" for m in ms)


@pytest.mark.parametrize("script", ["update-inner.sh", "install.sh"])
def test_keys_are_made_after_migrations_and_before_the_stack_starts(script: str) -> None:
    text = (_DEPLOY / script).read_text()
    ensure = text.index("python -m jbrain.cli owner-keys ensure")
    assert text.index("docker compose run --rm migrate") < ensure
    assert ensure < text.index(" up -d", ensure)
    # Never fatal: a failure must not abort an update or an install.
    line = text[text.rindex("\n", 0, ensure) : text.index("\n", ensure)]
    nxt = text[text.index("\n", ensure) :][:120]
    assert line.lstrip().startswith("if ") or nxt.lstrip().startswith("||")


def test_an_update_that_makes_keys_restarts_the_proxy() -> None:
    # Caddy reads certificate files only at start, and `up -d` leaves an unchanged proxy
    # running, so a renewed leaf would otherwise sit unused until the old one expired.
    text = (_DEPLOY / "update-inner.sh").read_text()
    ensure = text.index("python -m jbrain.cli owner-keys ensure")
    assert '*"made:"*) OWNER_KEYS_MADE=1' in text[ensure:]
    up = text.index(" up -d", ensure)
    restart = text.index("docker compose restart proxy", ensure)
    assert up < restart
    assert 'if [ -n "$OWNER_KEYS_MADE" ]' in text[up:restart]
