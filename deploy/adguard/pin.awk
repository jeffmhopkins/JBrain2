# Pin the three values jbrain owns in AdGuard Home's config, leaving the rest as found.
#
# AdGuard Home rewrites its own YAML whenever a setting changes, so the file on the
# volume is AdGuard's, not ours: blocklists and rules added through its API must
# survive a restart. Only these are re-asserted on every start, because each can drift
# under it without anyone touching AdGuard:
#
#   dns.bind_hosts  the box's LAN address, which moves if the router hands out a new
#                   lease. Never 0.0.0.0: systemd-resolved's stub already holds
#                   127.0.0.53:53 on Ubuntu, so a wildcard bind fails, and turning the
#                   stub off is a host step the owner cannot run (CLAUDE.md #10).
#   http.address    the docker bridge gateway, so the web UI and its API are reachable
#                   by the api (host.docker.internal) and never from the LAN.
#   local_ptr_upstreams  the router (the default gateway), for naming devices. Left to
#                   its default it asks systemd-resolved, which forwards to the DNS the
#                   router hands out: this box, so every reverse lookup loops. Skipped
#                   when no gateway was found (ptr empty).
#   users           one user, `jbrain`, whose bcrypt hash is of `adguard.token`. The api
#                   re-mints that token into an empty secrets volume, so the hash is
#                   recomputed every start rather than trusted from the file.
#
# AdGuard writes block sequences either at the parent's indent or two deeper, so a
# sequence item is any `- ` line, at whatever indent, until the next key.
#
# POSIX awk only: it runs under busybox in the image and under gawk/mawk in tests.
# Variables: dns (an IP), ui (IP:port), hash (a bcrypt hash), ptr (an IP or empty).

function toplevel(line) { return line ~ /^[A-Za-z_]/ }

{
	if (toplevel($0)) {
		in_users = 0
		in_binds = 0
		in_ptr = 0
		section = $0
		sub(/:.*/, "", section)
		if (section == "users") {
			print "users:"
			print "  - name: jbrain"
			print "    password: " hash
			in_users = 1
			seen_users = 1
			next
		}
		print
		next
	}
	if (in_users) next
	if (in_binds) {
		if ($0 ~ /^ *(- |#)/) next
		in_binds = 0
	}
	if (in_ptr) {
		if ($0 ~ /^ *(- |#)/) next
		in_ptr = 0
	}
	if (section == "dns" && ptr != "" && $0 ~ /^  local_ptr_upstreams:/) {
		print "  local_ptr_upstreams:"
		print "    - " ptr
		in_ptr = 1
		next
	}
	if (section == "dns" && $0 ~ /^  bind_hosts:/) {
		print "  bind_hosts:"
		print "    - " dns
		in_binds = 1
		next
	}
	if (section == "http" && $0 ~ /^  address:/) {
		print "  address: " ui
		next
	}
	print
}

# A config with no users block would serve the UI and its API to anyone who can reach
# the bridge, so a missing block is added rather than tolerated.
END {
	if (!seen_users) {
		print "users:"
		print "  - name: jbrain"
		print "    password: " hash
	}
}
