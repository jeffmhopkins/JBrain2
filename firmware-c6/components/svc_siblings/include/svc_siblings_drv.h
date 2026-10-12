/* The siblings service's device side, beside the app-facing svc_siblings.h: the main task hands
 * it the box's answer, the UI task applies it. The same file runs on the device and in the sim;
 * only the cache port differs (esp/sib_port_nvs.c, or sib_port_fake.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "sib_logic.h"

/* Before any app asks: the last list the box sent, from the cache, so the names show before the
 * box answers after a reboot. */
void svc_siblings_init(void);

/* Main task: the body of `GET /endpoint/siblings`. False when it is not the expected shape (the
 * slots stay as they are). Only hands the list over; the UI task applies it. */
bool svc_siblings_offer(const char *json, size_t len);
/* Main task: the fetch did not answer, or answered `status` other than 200. */
void svc_siblings_failed(int status);

/* UI task, once per loop pass: applies a list the main task left, clears a thread whose sibling
 * changed, and saves the slots to the cache when anything moved. */
void svc_siblings_service(void);

/* `sib=box:N,cache:N,fails:N,last:<status>` for telemetry. */
void svc_siblings_note(char *out, size_t len);

/* What each build supplies: the device's NVS blob, or the sim's RAM. */
bool sib_port_load(sib_slots_t *out);
void sib_port_save(const sib_slots_t *in);
