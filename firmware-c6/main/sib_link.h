/* The twins' panels' names from the box (`GET /endpoint/siblings`), for the two message threads.
 * MAIN TASK ONLY, like every box request; it only hands the answer to svc_siblings, which the UI
 * task applies. */
#pragma once

/* On the update loop's pass, after the manifest: fetches when this boot has no answer yet or the
 * last one is fifteen minutes old. */
void sib_link_poll(void);
