import importlib
import platform
import sys

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import test_sdr_demod as t  # noqa: E402


def test_diag():
    listen = importlib.import_module("listen")
    np.show_config()
    print(sys.version, platform.processor(), np.__version__, np.__file__)
    print(open("/proc/cpuinfo").read().split("\n\n")[0])
    for m in t.demod.IF_RATE_HZ:
        print("DIAG", m, t._dc_leak_db(m, listen.LISTEN_CAPTURE_HZ, 300_000.0), t._dc_leak_db(m, listen.LISTEN_CAPTURE_HZ, 240_000.0))
    built = t.demod.Demodulator("fm", listen.LISTEN_CAPTURE_HZ, offset_hz=300_000.0)
    print("DIAG mixer", built._mixer.offset_hz, built._mixer._period, [ (type(s).__name__, getattr(s, "_m", None), getattr(getattr(s, "_taps", None), "size", None), getattr(getattr(s, "_taps", None), "dtype", None)) for s in built._front])
    assert False
