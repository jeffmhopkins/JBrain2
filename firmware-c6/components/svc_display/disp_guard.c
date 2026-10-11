#include "disp_guard.h"

bool dg_may_start(int reset_reason)
{
    switch (reset_reason) {
    case DG_RST_PANIC:
    case DG_RST_INT_WDT:
    case DG_RST_TASK_WDT:
    case DG_RST_WDT:
    case DG_RST_CPU_LOCKUP:
    /* Waking a 480x480 AMOLED is the largest current step this board takes; a supply that
     * sags under it would brown out on every boot that tries. */
    case DG_RST_BROWNOUT:
    case DG_RST_PWR_GLITCH:
        return false;
    default:
        return true;
    }
}
