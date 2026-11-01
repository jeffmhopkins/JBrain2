# Pin the values jbrain owns in AdGuard Home's config, leaving the rest as found.
#
# AdGuard Home rewrites its own YAML whenever a setting changes, so the file on the
# volume is AdGuard's, not ours: blocklists and rules added through its API must
# survive a restart. Only these are re-asserted on every start, because each can drift
# under it, or be changed through its API, without anyone meaning to:
#
#   dns.bind_hosts  the box's LAN address, which moves if the router hands out a new
#                   lease. Never 0.0.0.0: systemd-resolved's stub already holds
#                   127.0.0.53:53 on Ubuntu, so a wildcard bind fails, and turning the
#                   stub off is a host step the owner cannot run (CLAUDE.md #10).
#   dns.allowed_clients  private ranges only. The bind is a private address, but a box
#                   in the router's DMZ, or behind a forwarded :53, still receives the
#                   internet's queries on it with their real sources; without this it
#                   would be an open resolver (and the seed turns the rate limit off).
#   dns.local_ptr_upstreams  the router (the default gateway), for naming devices. Left
#                   to its default it asks systemd-resolved, which forwards to the DNS
#                   the router hands out: this box, so every reverse lookup loops.
#                   Skipped when no gateway was found (ptr empty).
#   http.address    the docker bridge gateway, so the web UI and its API are reachable
#                   by the api (host.docker.internal) and no LAN device is told of it.
#   users           one user, `jbrain`, whose bcrypt hash is of `adguard.token`. The api
#                   re-mints that token into an empty secrets volume, so the hash is
#                   recomputed every start rather than trusted from the file.
#   a dead seed list  the first seed's HaGeZi list pointed at a jsDelivr `@latest` URL
#                   that answers 403, so a box seeded with it blocked from two lists.
#                   That one URL, and its name on the next line, become HaGeZi Pro from
#                   AdGuard's own registry; nothing else in `filters` is touched.
#
# AdGuard writes block sequences either at the parent's indent or two deeper, so a
# sequence item is any `- ` line, at whatever indent, until the next key.
#
# POSIX awk only: it runs under busybox in the image and under gawk/mawk in tests.
# Variables: dns (an IP), ui (IP:port), hash (a bcrypt hash), ptr (an IP or empty).

function toplevel(line) { return line ~ /^[A-Za-z_]/ }

function list(key, items,    n, parts, i) {
	print "  " key ":"
	n = split(items, parts, " ")
	for (i = 1; i <= n; i++) print "    - " parts[i]
}

function users() {
	print "users:"
	print "  - name: jbrain"
	print "    password: " hash
	seen_users = 1
}

# A dns block AdGuard wrote without the key still gets the private-only list.
function close_dns() {
	if (section == "dns" && !seen_allowed) {
		list("allowed_clients", PRIVATE)
		seen_allowed = 1
	}
}

BEGIN {
	PRIVATE = "127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fc00::/7 fe80::/10"
	DEAD_URL = "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/native.txt"
	LIVE_URL = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_48.txt"
	LIVE_NAME = "HaGeZi Pro"
}

{
	if (toplevel($0)) {
		close_dns()
		rename = 0
		in_users = 0
		in_list = 0
		section = $0
		sub(/:.*/, "", section)
		if (section == "users") {
			users()
			in_users = 1
			next
		}
		print
		next
	}
	if (in_users) next
	if (in_list) {
		if ($0 ~ /^ *(- |#)/) next
		in_list = 0
	}
	if (section == "dns" && $0 ~ /^  bind_hosts:/) {
		list("bind_hosts", dns)
		in_list = 1
		next
	}
	if (section == "dns" && $0 ~ /^  allowed_clients:/) {
		list("allowed_clients", PRIVATE)
		seen_allowed = 1
		in_list = 1
		next
	}
	if (section == "dns" && ptr != "" && $0 ~ /^  local_ptr_upstreams:/) {
		list("local_ptr_upstreams", ptr)
		in_list = 1
		next
	}
	if (section == "filters" && index($0, "url: " DEAD_URL)) {
		sub(/url: .*/, "url: " LIVE_URL)
		print
		rename = 1
		next
	}
	# The flag lives only inside the dead entry: a new item or key ends it, so an entry
	# without a name can never rename something further down.
	if (rename && $0 ~ /^ *- /) rename = 0
	if (rename && $0 ~ /^ *name: /) {
		sub(/name: .*/, "name: " LIVE_NAME)
		print
		rename = 0
		next
	}
	if (section == "http" && $0 ~ /^  address:/) {
		print "  address: " ui
		next
	}
	print
}

# A config with no users block would serve the UI and its API to anything that can reach
# the bridge, so a missing block is added rather than tolerated.
END {
	close_dns()
	if (!seen_users) users()
}
