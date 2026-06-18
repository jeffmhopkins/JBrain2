"""MQTT secure spine (Phase 7+ / JBrain360 M0).

Authorization for the self-hosted Mosquitto broker. The broker runs
mosquitto-go-auth in its **HTTP backend** mode: on every connect it POSTs to
`/internal/mqtt-auth` and on every publish/subscribe to `/internal/mqtt-acl`
(see `jbrain.api.mqtt`). The plugin therefore holds **zero** credential logic —
device authentication reuses the shipped `device_key` model verbatim and the
ACL is decided here, under the same Postgres/RLS firewall as the rest of the box
(plan invariants T2, T8).
"""
