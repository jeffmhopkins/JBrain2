# JBrain360 operations runbook

Operating the family-location surface (Phase 7) safely: the controls the owner
runs, and the deploy-time invariants the in-app security rests on. The in-database
firewall (RLS), the content-free push, and the WebView lockdown are covered in
`ARCHITECTURE.md`; this is the **operator's** view — revoking access, the
encryption-at-rest compensating control, and rotating the two long-lived secrets
(the device Keystore key and the server's pinned cert).

## Revoking a member

`POST /api/family/members/{subject_id}/revoke` (owner-only) is the single control
that cuts a member off everywhere at once (M7b):

- **Dashboard cookie → instant 401.** Revoke tombstones the device principal
  (`principals.revoked_at`), and the session lookup filters `revoked_at IS NULL`,
  so the next dashboard request fails authentication immediately — no waiting on
  cookie expiry.
- **MQTT live session → dropped within bound.** `mqtt-auth` already denies the
  next connect; the per-publish `mqtt-acl` re-check denies a revoked device **even
  on its own namespace**, so an already-connected phone is disconnected on its next
  publish (bounded by the broker's ACL-cache TTL — keep it short).
- **History reads → gone.** The member is dropped from the family group
  (`view_scope`), so `viewer_may_see` returns false: no more family-sees-family
  reads in either direction.

There is **no in-app "Leave" button** (T7): the Android-mandated persistent
tracking notification already discloses "you are being tracked", and self-revocation
is the owner's call. Removing a member without revoking the device key
(`DELETE /api/family/members/{id}`) ends the *sharing* but leaves the device able
to publish its own location; use `…/revoke` to also kill the credential.

## Encryption at rest (compensating control)

There is **no data-retention cap, no per-domain gate, and no self-Leave** for
location history (T7) — so the compensating control is that the data is encrypted
at rest. This is a **deploy-host invariant**, not application code:

1. Put the Postgres data volume (and the blob/backup dirs) on an **encrypted
   filesystem** — LUKS on the data disk, or a cloud provider's encrypted volume.
   On a single self-hosted box, LUKS with a passphrase entered at boot is the
   baseline; an unattended box should use a TPM-sealed key.
2. The Docker named volumes (`pgdata`, blobs, backups in `docker-compose.yml`)
   inherit the host filesystem's encryption — there is nothing to configure in
   compose beyond ensuring the Docker root / those volumes live on the encrypted
   mount.
3. **Backups carry the same data.** `deploy/backup.sh` output must land on
   encrypted storage too; never copy a backup to an unencrypted disk or an
   un-encrypted cloud bucket.

This does not defend against a live-server compromise (the data is decrypted in
use) — it defends a **stolen disk / decommissioned drive / lifted backup**, which
is the threat retention limits would otherwise bound.

## Device Keystore key — backup & escrow

The device key (the credential a phone exchanges for its dashboard session and uses
to publish location) lives in the **Android Keystore** and is **non-exportable by
design** (plan B8): it never leaves the secure element, never reaches page
JavaScript, and cannot be backed up off the device.

There is therefore nothing to *escrow* — recovery is **re-pairing**, not key
restore:

- **Lost / replaced phone:** the owner mints a fresh pairing code
  (`POST /api/pairing/codes`) and redeems it on the new device. The old device's
  principal should be **revoked** (above) so its key is dead.
- **The owner key is the root of trust.** It mints pairing codes and is the only
  credential that can re-provision devices, so *it* is what must be backed up — keep
  the owner key in a password manager / hardware token, offline. Losing it means
  re-bootstrapping (`scripts`/`deploy/install.sh` owner-key rotation), not data loss.
- Pairing codes are **one-time and short-lived**; never reuse or store a redeemed
  code.

## Server cert SPKI pin rotation

The app pins the server's certificate **SPKI** (subject public-key info) so a
mis-issued or MITM cert is rejected even if it chains to a trusted CA (plan B8). The
pin is a hash of the **public key**, so it survives a cert *renewal* only when the
key is reused — and rotating it requires shipping an app build, so plan ahead:

1. **Keep the key stable across renewals.** Configure the TLS terminator (Caddy in
   direct mode, or the edge in Cloudflare-tunnel mode) to **reuse the certificate
   key** on renewal rather than generating a fresh one, so a 90-day renewal does not
   silently break every installed app. Pin the **leaf key** you control, not the
   CA.
2. **Rotating the key (planned):** ship app build *N+1* that pins **both** the old
   and the new SPKI. Wait for adoption (the old build keeps working on the old key).
   Once telemetry shows N+1 is universal, cut the server over to the new key and
   retire the old pin in build *N+2*. Never single-pin a key you are about to
   replace — that bricks every un-updated phone.
3. **Emergency rotation (key compromised):** there is no graceful path — cut to a
   new key immediately and push an app update; un-updated devices fail closed
   (refuse to connect), which is the correct security outcome.

Keep at least one **backup pin** (a second key held offline) baked into every build
so an emergency rotation has a pre-trusted target.
