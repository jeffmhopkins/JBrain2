/* SHA-256 for the host suite alone (FIPS 180-4): the box's fixtures name their pixels and sound
 * by digest (backend/tests/fixtures/jpanel_notice/vectors.json), and the device's mbedTLS is not
 * here. */
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t h[8];
    uint64_t len;
    uint8_t buf[64];
    size_t n;
} sha256_t;

void sha256_init(sha256_t *s);
void sha256_update(sha256_t *s, const void *data, size_t n);
/* The digest as 64 lower-case hex characters and a terminator. */
void sha256_hex(sha256_t *s, char out[65]);
