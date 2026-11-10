"""What PWA flashing writes to a GrowBot (docs/plans/GROWBOT_PLAN.md §3): the file map and the
generated `secrets.py`.

`secrets.py` is read twice on the robot: imported as Python by `relay_chip.py` (main.py), and
parsed by `wifi_store.parse_config`, a restricted literal parser that accepts strings, ints and
lists but no `b"..."` bytes. So every value here is one of those, strings are quoted by the
same rules as `wifi_store.replacement`'s `quoted()`, and the Wi-Fi list sits in the managed
`# BEGIN GROWBOT WIFI V1` block that the robot's own Wi-Fi setup later edits in place. The
names (`GROWBOT_HOST`, `GROWBOT_PORT`, `GROWBOT_SNI`, `GROWBOT_KEY`, `GROWBOT_CA`,
`GROWBOT_NTP_PORT`) are the ones `growbot/patches/apply.py` patches the firmware to read; the CA
is DER as base64 because the firmware decodes it with `binascii.a2b_base64`, and the NTP port is
the growbot service's SNTP, which sets the robot's clock before it checks the CA's dates.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import ssl

# Repo file -> name on the robot. relay_chip.py is upstream's main program, so it boots as
# main.py. main.py goes last: a copy cut off part-way leaves the old main program, not a new
# one beside modules it does not match.
FILE_MAP: tuple[tuple[str, str], ...] = (
    ("act_engine.py", "act_engine.py"),
    ("PicoRobotics.py", "PicoRobotics.py"),
    ("wifi_store.py", "wifi_store.py"),
    ("wifi_recovery.py", "wifi_recovery.py"),
    ("relay_chip.py", "main.py"),
)

BEGIN = "# BEGIN GROWBOT WIFI V1"
END = "# END GROWBOT WIFI V1"
# wifi_store's limits: MAX_NETWORKS and MAX_CONFIG.
MAX_NETWORKS = 8
# The growbot service's SNTP (growbot/server/serve.py; compose publishes it on udp).
NTP_PORT = 8123
MAX_CONFIG = 16384

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_DOCKER = ipaddress.ip_network("172.16.0.0/12")


class ProvisionError(ValueError):
    """A value the robot would refuse, as a sentence the owner can act on."""


def validate_network(ssid: str, password: str) -> tuple[str, str]:
    """wifi_store.validate_network's rules, so the box never writes a network the robot
    rejects: a 1–32 byte UTF-8 SSID with no NUL, an 8–63 character printable-ASCII WPA
    passphrase (no open networks, no raw 64-hex PSK, nothing trimmed)."""
    try:
        size = len(ssid.encode("utf-8"))
    except UnicodeEncodeError:
        raise ProvisionError("That network name is not valid text.") from None
    if not 1 <= size <= 32 or "\x00" in ssid:
        raise ProvisionError("A Wi-Fi network name is 1 to 32 bytes long.")
    if not 8 <= len(password) <= 63 or any(not 32 <= ord(c) <= 126 for c in password):
        raise ProvisionError(
            f"The password for {ssid} must be 8 to 63 plain characters (letters, digits"
            " and punctuation): the robot joins WPA2 personal networks only."
        )
    return ssid, password


def quoted(value: str) -> str:
    """wifi_store's quoted(): a Python string literal both parsers read back exactly."""
    out = '"'
    for char in value:
        if char in ('"', "\\"):
            out += "\\" + char
        elif ord(char) < 32:
            out += f"\\u{ord(char):04x}"
        else:
            out += char
    return out + '"'


def ca_base64(ca_pem: str) -> str:
    """The first certificate of a PEM as one line of base64 DER."""
    try:
        der = ssl.PEM_cert_to_DER_cert(ca_pem)
    except ValueError:
        raise ProvisionError("The box's root certificate could not be read.") from None
    return base64.b64encode(der).decode()


def render_secrets(
    host: str,
    port: int,
    sni: str,
    key: str,
    ca_pem: str,
    networks: list[tuple[str, str]],
) -> str:
    """The robot's secrets.py, exactly the lines the firmware reads and nothing else."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        raise ProvisionError("The box's address must be an IPv4 address.") from None
    if not _HOSTNAME.fullmatch(sni):
        raise ProvisionError("The box's LAN name is not a hostname.")
    if not KEY_RE.fullmatch(key):
        raise ProvisionError("The device key is malformed.")
    if not 0 < port < 65536:
        raise ProvisionError("The box's port is out of range.")
    if not networks:
        raise ProvisionError("Give the robot at least one Wi-Fi network.")
    if len(networks) > MAX_NETWORKS:
        raise ProvisionError(f"The robot keeps at most {MAX_NETWORKS} networks.")
    seen: set[str] = set()
    for ssid, password in networks:
        validate_network(ssid, password)
        if ssid in seen:
            raise ProvisionError(f"{ssid} is listed twice.")
        seen.add(ssid)
    wifi = ", ".join(f"[{quoted(s)}, {quoted(p)}]" for s, p in networks)
    text = (
        f"GROWBOT_HOST = {quoted(host)}\n"
        f"GROWBOT_PORT = {int(port)}\n"
        f"GROWBOT_SNI = {quoted(sni)}\n"
        f"GROWBOT_KEY = {quoted(key)}\n"
        f"GROWBOT_CA = {quoted(ca_base64(ca_pem))}\n"
        f"GROWBOT_NTP_PORT = {NTP_PORT}\n"
        f"{BEGIN}\n"
        f"WIFI_NETWORKS = [{wifi}]\n"
        f"{END}\n"
    )
    if len(text.encode("utf-8")) > MAX_CONFIG:
        raise ProvisionError("Those networks make secrets.py too large for the robot.")
    return text


def pick_box_host(dns_addresses: object) -> str | None:
    """The box's LAN IPv4 from AdGuard's `dns_addresses`: the first private address that is
    not Docker's 172.16/12, loopback or link-local, since the robot dials it from the LAN."""
    if not isinstance(dns_addresses, list):
        return None
    for raw in dns_addresses:
        try:
            addr = ipaddress.ip_address(str(raw))
        except ValueError:
            continue
        if (
            isinstance(addr, ipaddress.IPv4Address)
            and addr.is_private
            and not addr.is_loopback
            and not addr.is_link_local
            and not addr.is_unspecified
            and addr not in _DOCKER
        ):
            return str(addr)
    return None
