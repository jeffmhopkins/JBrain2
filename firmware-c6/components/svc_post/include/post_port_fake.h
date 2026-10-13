/* The host's stand-in for the stamps' NVS blob (post_port_fake.c). */
#pragma once

/* A first boot: nothing kept. */
void post_port_fake_wipe(void);
/* How many times the list was written: a burst of plays is one write. */
unsigned post_port_fake_saves(void);
