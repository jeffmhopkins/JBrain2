/* The device's side of svc_net.h. The radio is joined and re-joined by the update loop
 * (main/net.c); this service only tells the glass what the radio is doing and carries the
 * owner's Connect / Disconnect to that loop. */
#pragma once

#include <stdbool.h>

#include "svc_net.h"

/* `wake` notifies the update loop when Connect is pressed; it must only notify. */
void svc_net_start(void (*wake)(void));
/* main/net.c's event handler and the update loop: the radio's state as it changes. Raises
 * NET_UP / NET_DOWN on the edges. Any task. */
void svc_net_set_state(net_state_t state);
/* The owner pressed Disconnect and has not pressed Connect since: nothing may re-join. Lasts
 * until Connect or a restart, never across one. */
bool svc_net_user_off(void);
/* Main task: true once per Connect press. */
bool svc_net_take_connect(void);
