"""The owner site and the ingress/device headers (docs/plans/OWNER_TUNNEL_PLAN.md §3.2).

Two halves. The render script is run under `sh` against throwaway dirs. Then the real thing:
the repo's Caddyfile and every rendered site run in a real Caddy against a header-echoing
upstream, so what the api receives is observed, not inferred from config text. That half
needs a `caddy` binary on PATH (CI installs a pinned one, `.github/actions/setup-caddy`, and
sets JBRAIN_REQUIRE_CADDY so a missing binary fails instead of skipping).

Security path: these headers are how the api will tell an enrolled phone from the internet.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from jbrain.owner_tunnel import keys
from jbrain.storage import FsOwnerKeyFiles

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_RENDER = _DEPLOY / "proxy-owner-conf.sh"
_FORGED = {"X-JBrain-Ingress": "owner-tunnel", "X-JBrain-Device": "f" * 64}


@pytest.fixture
def key_dirs(tmp_path: Path) -> tuple[FsOwnerKeyFiles, Path]:
    files = FsOwnerKeyFiles(tmp_path / "keys", tmp_path / "site")
    keys.ensure(files)
    return files, tmp_path / "site"


def _render(out: Path, tls: Path) -> str | None:
    subprocess.run(["sh", str(_RENDER), str(out), str(tls)], check=True)
    rendered = out / "owner.caddy"
    return rendered.read_text() if rendered.exists() else None


# --- the render script ------------------------------------------------------------------


def test_no_site_until_every_file_exists(tmp_path: Path, key_dirs: tuple) -> None:
    _, site = key_dirs
    for missing in ("site.crt", "site.key", "enroll-ca.crt"):
        held = (site / missing).read_bytes()
        (site / missing).unlink()
        assert _render(tmp_path / "out", site) is None
        (site / missing).write_bytes(held)
    (site / "site.key").write_bytes(b"")
    assert _render(tmp_path / "out", site) is None


def test_site_demands_an_enrolled_client(tmp_path: Path, key_dirs: tuple) -> None:
    _, site = key_dirs
    rendered = _render(tmp_path / "out", site)
    assert rendered is not None
    assert rendered.startswith("https://owner.jbrain.home.arpa:8443 {")
    assert "mode require_and_verify" in rendered
    assert f"trust_pool file {site}/enroll-ca.crt" in rendered
    assert "import app owner-tunnel {http.request.tls.client.public_key_sha256}" in rendered


def test_a_vanished_key_tears_the_site_down(tmp_path: Path, key_dirs: tuple) -> None:
    _, site = key_dirs
    assert _render(tmp_path / "out", site) is not None
    (site / "site.key").unlink()
    assert _render(tmp_path / "out", site) is None
    assert list((tmp_path / "out").iterdir()) == []


def test_every_site_sets_both_headers() -> None:
    caddyfile = (_DEPLOY / "Caddyfile").read_text()
    snippet = caddyfile[caddyfile.index("(app) {") : caddyfile.index("{$JBRAIN_SITE_ADDR} {")]
    assert "header_up X-JBrain-Ingress {args[0]}" in snippet
    assert 'header_up X-JBrain-Device "{args[1]}"' in snippet
    assert 'import app public ""' in caddyfile
    assert 'import app lan ""' in (_DEPLOY / "proxy-lan-conf.sh").read_text()
    preview = (_DEPLOY / "proxy-preview-conf.sh").read_text()
    assert "header_up X-JBrain-Ingress preview" in preview
    assert 'header_up X-JBrain-Device ""' in preview
    assert "import /etc/caddy/owner/*.caddy" in caddyfile


def test_proxy_reads_the_site_volume_only() -> None:
    import yaml

    proxy = yaml.safe_load((_DEPLOY / "docker-compose.yml").read_text())["services"]["proxy"]
    assert "owner_site:/etc/caddy/owner-tls:ro" in proxy["volumes"]
    assert not any(str(v).startswith("owner_keys:") for v in proxy["volumes"])
    assert all("8443" not in str(p) for p in proxy["ports"])
    entry = (_DEPLOY / "proxy-entrypoint.sh").read_text()
    assert "proxy-owner-conf.sh /etc/caddy/owner /etc/caddy/owner-tls" in entry
    assert "proxy-owner-conf.sh" in (_DEPLOY / "Dockerfile.proxy").read_text()


# --- real Caddy ---------------------------------------------------------------------------


def _caddy() -> str:
    found = shutil.which("caddy")
    if found is None:
        if os.environ.get("JBRAIN_REQUIRE_CADDY"):
            pytest.fail("caddy is not on PATH but JBRAIN_REQUIRE_CADDY is set")
        pytest.skip("no caddy binary (scripts/dev-setup.sh installs one)")
    return found


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Echo(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:
        _Echo.hits += 1
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None


class _Stack:
    def __init__(self, root: Path, site_dir: Path, files: FsOwnerKeyFiles) -> None:
        self.files = files
        self.ports = {name: _free_port() for name in ("public", "http", "lan", "owner")}
        echo = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        self._echo = echo
        threading.Thread(target=echo.serve_forever, daemon=True).start()
        etc = root / "etc"
        www = root / "www"
        www.mkdir()
        (www / "index.html").write_text("app")
        env = {
            **os.environ,
            "JBRAIN_SITE_ADDR": f"http://localhost:{self.ports['public']}",
            "JBRAIN_LAN_ADDR": f"https://jbrain.local:{self.ports['lan']}",
            "JBRAIN_JCODE_PREVIEW_BASE_HOST": "preview.test",
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CONFIG_HOME": str(root / "config"),
        }
        for script, sub in (("proxy-lan-conf.sh", "lan"), ("proxy-preview-conf.sh", "preview")):
            subprocess.run(["sh", str(_DEPLOY / script), str(etc / sub)], check=True, env=env)
        subprocess.run(["sh", str(_RENDER), str(etc / "owner"), str(site_dir)], check=True)
        owner = etc / "owner" / "owner.caddy"
        owner.write_text(owner.read_text().replace(":8443", f":{self.ports['owner']}"))
        upstream = f"127.0.0.1:{echo.server_address[1]}"
        caddyfile = (_DEPLOY / "Caddyfile").read_text()
        caddyfile = caddyfile.replace("/etc/caddy/", f"{etc}/").replace("api:8000", upstream)
        caddyfile = caddyfile.replace("/srv/www", str(www))
        for sub in ("lan", "preview"):
            conf = next((etc / sub).glob("*.caddy"))
            conf.write_text(conf.read_text().replace("api:8000", upstream))
        caddyfile = caddyfile.replace(":8443", f":{self.ports['owner']}")
        # Into the Caddyfile's own global block, which keeps the owner listener's options.
        test_opts = (
            "\n{\n\tadmin off\n\tskip_install_trust\n\tauto_https disable_redirects\n"
            f"\thttp_port {self.ports['http']}\n\thttps_port {_free_port()}\n"
        )
        assert "\n{\n" in caddyfile
        self.config = root / "Caddyfile"
        self.config.write_text(caddyfile.replace("\n{\n", test_opts, 1))
        self.env = env
        self.caddy = _caddy()
        self.proc: subprocess.Popen[bytes] | None = None

    def validate(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.caddy, "validate", "--config", str(self.config), "--adapter", "caddyfile"],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [self.caddy, "run", "--config", str(self.config), "--adapter", "caddyfile"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 20
        for port in self.ports.values():
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    if self.proc.poll() is not None or time.monotonic() > deadline:
                        err = self.proc.stderr.read().decode() if self.proc.stderr else ""
                        raise AssertionError(f"caddy did not start: {err[-2000:]}") from None
                    time.sleep(0.1)

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(10)
        self._echo.shutdown()

    def get(self, port: str, host: str, *, tls: ssl.SSLContext | None = None) -> dict[str, str]:
        head, body = self.request(port, host, tls=tls)
        assert head.startswith(b"HTTP/1.1 200"), head
        return json.loads(body)

    def request(
        self,
        port: str,
        host: str,
        *,
        tls: ssl.SSLContext | None = None,
        sni: str | None = "",
    ) -> tuple[bytes, bytes]:
        """One request; `sni` defaults to the Host's name, None sends no SNI at all."""
        raw = socket.create_connection(("127.0.0.1", self.ports[port]), timeout=5)
        conn: socket.socket = raw
        if tls is not None:
            name = host.split(":")[0] if sni == "" else sni
            conn = tls.wrap_socket(raw, server_hostname=name)
        forged = "".join(f"{k}: {v}\r\n" for k, v in _FORGED.items())
        request = f"GET /api/echo HTTP/1.1\r\nHost: {host}\r\n{forged}Connection: close\r\n\r\n"
        with conn:
            conn.sendall(request.encode())
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
        if not data:
            raise ConnectionError("closed without a response")
        head, _, body = data.partition(b"\r\n\r\n")
        return head, body


@pytest.fixture
def stack(tmp_path: Path, key_dirs: tuple) -> Iterator[_Stack]:
    files, site = key_dirs
    s = _Stack(tmp_path, site, files)
    yield s
    s.stop()


def _client_cert(signer_key: bytes, signer_cert: bytes) -> tuple[bytes, bytes, str]:
    """A phone's certificate signed by the given CA: (cert PEM, key PEM, SPKI hash)."""
    ca_key = serialization.load_pem_private_key(signer_key, None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    ca = x509.load_pem_x509_certificate(signer_cert)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phone")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key_pem,
        keys.public_key_sha256(key.public_key()),
    )


def _owner_client(stack: _Stack, tmp: Path, cert: tuple[bytes, bytes] | None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=(stack.files.read("owner-ca.crt") or b"").decode())
    if cert is not None:
        (tmp / "c.crt").write_bytes(cert[0])
        (tmp / "c.key").write_bytes(cert[1])
        ctx.load_cert_chain(tmp / "c.crt", tmp / "c.key")
    return ctx


def test_rendered_config_validates(stack: _Stack) -> None:
    result = stack.validate()
    assert result.returncode == 0, result.stderr[-2000:]


def test_no_site_lets_a_client_choose_its_headers(stack: _Stack, tmp_path: Path) -> None:
    stack.start()
    public = stack.get("public", f"localhost:{stack.ports['public']}")
    assert (public["x-jbrain-ingress"], public["x-jbrain-device"]) == ("public", "")

    lan_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    lan_tls.check_hostname = False
    lan_tls.verify_mode = ssl.CERT_NONE
    lan = stack.get("lan", f"jbrain.local:{stack.ports['lan']}", tls=lan_tls)
    assert (lan["x-jbrain-ingress"], lan["x-jbrain-device"]) == ("lan", "")

    preview = stack.get("http", "0123abcd-preview.preview.test")
    assert (preview["x-jbrain-ingress"], preview["x-jbrain-device"]) == ("preview", "")

    signed = _client_cert(
        stack.files.read("enroll-ca.key") or b"", stack.files.read("enroll-ca.crt") or b""
    )
    owner = stack.get(
        "owner", keys.OWNER_HOST, tls=_owner_client(stack, tmp_path, (signed[0], signed[1]))
    )
    # Caddy's placeholder and keys.public_key_sha256 agree, so the api can match it to a device row.
    assert (owner["x-jbrain-ingress"], owner["x-jbrain-device"]) == ("owner-tunnel", signed[2])


def test_owner_site_refuses_a_client_without_an_enrolled_cert(
    stack: _Stack, tmp_path: Path
) -> None:
    stack.start()
    with pytest.raises((ssl.SSLError, ConnectionError)):
        stack.request("owner", keys.OWNER_HOST, tls=_owner_client(stack, tmp_path, None))
    # A certificate from any other CA (here the owner CA, which signs servers) is refused too.
    stranger = _client_cert(
        stack.files.read("owner-ca.key") or b"", stack.files.read("owner-ca.crt") or b""
    )
    with pytest.raises((ssl.SSLError, ConnectionError)):
        stack.request(
            "owner",
            keys.OWNER_HOST,
            tls=_owner_client(stack, tmp_path, (stranger[0], stranger[1])),
        )


def test_owner_site_speaks_tls13_with_the_owner_leaf(stack: _Stack, tmp_path: Path) -> None:
    stack.start()
    signed = _client_cert(
        stack.files.read("enroll-ca.key") or b"", stack.files.read("enroll-ca.crt") or b""
    )
    ctx = _owner_client(stack, tmp_path, (signed[0], signed[1]))
    raw = socket.create_connection(("127.0.0.1", stack.ports["owner"]), timeout=5)
    with ctx.wrap_socket(raw, server_hostname=keys.OWNER_HOST) as tls:
        assert tls.version() == "TLSv1.3"
        der = tls.getpeercert(binary_form=True)
        assert der is not None
    leaf = x509.load_der_x509_certificate(der)
    assert leaf == x509.load_pem_x509_certificates(stack.files.read("site.crt") or b"")[0]


@pytest.mark.parametrize("sni", ["jbrain.local", None], ids=["foreign-sni", "no-sni"])
def test_owner_host_under_another_name_never_reaches_the_api(
    stack: _Stack, tmp_path: Path, sni: str | None
) -> None:
    # The :8443 listener also has a catch-all TLS policy without client auth, so a cert-less
    # handshake under another name (or none) succeeds. What must stop it is strict SNI: the
    # request for the owner host on that connection is refused before it is proxied.
    stack.start()
    ctx = _owner_client(stack, tmp_path, None)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    before = _Echo.hits
    try:
        head, _ = stack.request("owner", keys.OWNER_HOST, tls=ctx, sni=sni)
    except (ssl.SSLError, ConnectionError):
        head = b""
    assert not head.startswith(b"HTTP/1.1 2"), head
    assert _Echo.hits == before


def test_owner_site_refuses_tls12(stack: _Stack, tmp_path: Path) -> None:
    stack.start()
    signed = _client_cert(
        stack.files.read("enroll-ca.key") or b"", stack.files.read("enroll-ca.crt") or b""
    )
    ctx = _owner_client(stack, tmp_path, (signed[0], signed[1]))
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises((ssl.SSLError, ConnectionError)):
        stack.request("owner", keys.OWNER_HOST, tls=ctx)


def test_a_mismatched_owner_pair_fails_validation(stack: _Stack) -> None:
    # What the entrypoint's validate-and-drop guards against: a site key that is not the
    # certificate's would stop Caddy loading every site, not just the owner one.
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    stack.files.write("site.key", other)
    assert stack.validate().returncode != 0
    entry = (_DEPLOY / "proxy-entrypoint.sh").read_text()
    check = entry.index("caddy validate --config /etc/caddy/Caddyfile")
    assert entry.index("rm -f /etc/caddy/owner/owner.caddy", check) < entry.index("exec caddy run")


def test_the_box_runs_the_caddy_the_tests_run() -> None:
    root = _DEPLOY.parent
    image = (_DEPLOY / "Dockerfile.proxy").read_text()
    action = (root / ".github" / "actions" / "setup-caddy" / "action.yml").read_text()
    version = re.search(r"CADDY_VERSION: (\S+)", action)
    assert version is not None
    assert f"FROM caddy:{version[1]}\n" in image
