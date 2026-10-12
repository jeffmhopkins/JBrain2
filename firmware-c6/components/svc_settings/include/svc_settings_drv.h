/* The device's settings service: the panel's choices in NVS, beside the app-facing
 * svc_settings.h. It is the ONE writer of its namespace. Nothing here can stop a boot: a
 * namespace that is missing or unreadable leaves every setting at its default. */
#pragma once

#include <stddef.h>
#include <stdint.h>

/* Loads the `jpanel` namespace. The box's provisioning (`jbrain`) is never written here. */
void svc_settings_init(void);

/* What the box's flasher wrote, as pairs that outlive the program (main's cfg); a NULL value is
 * a key the box did not write. Each secret value is registered with the debug writer's
 * scrubber. */
typedef struct {
    const char *key, *value;
} settings_kv_t;
void svc_settings_provisioning(const settings_kv_t *kv, unsigned n);

/* The UI task, every pass: writes what is owed once the panel has been left alone
 * (settings_store.h). */
void svc_settings_service(uint32_t now_ms);

/* `set=ok:N bri=V/nvs set_w=N set_err=…` — telemetry-safe. */
void svc_settings_note(char *out, size_t len);
