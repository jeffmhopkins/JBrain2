/* The alarms with the box: `GET /endpoint/alarms` for the owner's list, `PUT /endpoint/alarms/{id}`
 * for a switch flipped on the glass. MAIN TASK ONLY, like every box request; it only hands the
 * answers to svc_alarms, which the UI task applies. */
#pragma once

/* On every pass of the update loop, after the manifest (a minute; at once when a switch on the
 * glass wakes the loop): the waiting switches first, then the list with the etag the panel holds
 * — a 304 and no body when nothing changed. */
void alarm_link_poll(void);
