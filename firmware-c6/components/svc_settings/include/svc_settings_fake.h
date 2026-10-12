/* In-memory settings and provisioning: the sim's and the host suite's (the device's is
 * esp/svc_settings_nvs.c). The provisioning values are fakes shaped like the real ones, secrets
 * included, so the redaction test has something real to catch. */
#pragma once

#define SETTINGS_FAKE_PASS "fake-wifi-passphrase"
#define SETTINGS_FAKE_TOKEN "dk_fake_device_key_0123456789"
#define SETTINGS_FAKE_CA "-----BEGIN CERTIFICATE-----fakeboxroot"

void svc_settings_init(void);
