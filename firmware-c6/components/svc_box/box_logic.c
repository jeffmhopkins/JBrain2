#include "box_logic.h"

bool box_busy(fw_phase_t phase)
{
    return phase == FW_CHECKING || phase == FW_DOWNLOADING || phase == FW_RESTARTING;
}

bool box_may_update(net_state_t net, fw_phase_t phase)
{
    return net == NET_CONNECTED && !box_busy(phase);
}
