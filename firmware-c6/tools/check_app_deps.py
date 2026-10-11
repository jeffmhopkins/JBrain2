#!/usr/bin/env python3
"""Every app stands alone (JPANEL_C6_PLAN.md §1): an app_* component may depend on app_api,
ui_kit and LVGL and on nothing else — not the shell, not another app, not a service's
implementation, not a chip. Two ways in, both checked:

- its CMakeLists REQUIRES / PRIV_REQUIRES, which is what ESP-IDF links;
- its #includes, because the host sim compiles every component with every include path and
  would never notice an app reaching into another.

  firmware-c6/tools/check_app_deps.py [components-dir]
  firmware-c6/tools/check_app_deps.py --self-test
"""

import os
import re
import sys
import tempfile

ALLOWED_COMPONENTS = {"app_api", "ui_kit", "lvgl", "lvgl__lvgl"}
SHARED_INCLUDE_DIRS = ("app_api/include", "ui_kit/include")
REQ_RE = re.compile(r"\b(?:PRIV_)?REQUIRES\b([^)]*?)(?=\b[A-Z_]{4,}\b|\))", re.S)
INC_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]', re.M)


def requirements(cmake_text):
    names = set()
    for m in REQ_RE.finditer(cmake_text):
        names.update(t for t in m.group(1).split() if not t.startswith("#"))
    return names


def check(components):
    problems = []
    shared = set()
    for d in SHARED_INCLUDE_DIRS:
        path = os.path.join(components, d)
        if os.path.isdir(path):
            shared.update(os.listdir(path))
    shared.add("lvgl.h")
    apps = sorted(d for d in os.listdir(components) if d.startswith("app_") and d != "app_api")
    for app in apps:
        root = os.path.join(components, app)
        cmake = os.path.join(root, "CMakeLists.txt")
        if not os.path.exists(cmake):
            problems.append(f"{app}: no CMakeLists.txt")
            continue
        with open(cmake) as f:
            extra = requirements(f.read()) - ALLOWED_COMPONENTS
        if extra:
            problems.append(f"{app}/CMakeLists.txt requires {', '.join(sorted(extra))}")
        own = set()
        for dirpath, _, files in os.walk(root):
            own.update(f for f in files if f.endswith(".h"))
        for dirpath, _, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith((".c", ".h")):
                    continue
                path = os.path.join(dirpath, name)
                with open(path) as f:
                    text = f.read()
                for kind, header in INC_RE.findall(text):
                    if kind == "<":
                        continue
                    if header in own or header in shared:
                        continue
                    rel = os.path.relpath(path, components)
                    problems.append(f'{rel} includes "{header}"')
    return apps, problems


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        for d in ("app_api/include", "ui_kit/include", "app_good/include", "app_bad"):
            os.makedirs(os.path.join(tmp, d))
        open(os.path.join(tmp, "app_api/include/app_api.h"), "w").close()
        open(os.path.join(tmp, "app_good/include/app_good.h"), "w").close()
        with open(os.path.join(tmp, "app_good/CMakeLists.txt"), "w") as f:
            f.write('idf_component_register(SRCS "g.c" INCLUDE_DIRS "include"\n'
                    "    REQUIRES app_api ui_kit)\n")
        with open(os.path.join(tmp, "app_good/g.c"), "w") as f:
            f.write('#include <string.h>\n#include "app_api.h"\n#include "app_good.h"\n')
        with open(os.path.join(tmp, "app_bad/CMakeLists.txt"), "w") as f:
            f.write('idf_component_register(SRCS "b.c"\n    REQUIRES app_api shell\n'
                    "    PRIV_REQUIRES app_good)\n")
        with open(os.path.join(tmp, "app_bad/b.c"), "w") as f:
            f.write('#include "app_good.h"\n#include "svc_rtc_fake.h"\n')
        _, problems = check(tmp)
    want = [
        "app_bad/CMakeLists.txt requires app_good, shell",
        'app_bad/b.c includes "app_good.h"',
        'app_bad/b.c includes "svc_rtc_fake.h"',
    ]
    if problems != want:
        print("self-test FAILED:\n  got  " + "\n       ".join(problems), file=sys.stderr)
        return 1
    print("self-test ok: a clean app passes; another app, the shell and a fake are all caught")
    return 0


def main(argv):
    if argv[1:] == ["--self-test"]:
        return self_test()
    here = os.path.dirname(os.path.abspath(__file__))
    components = argv[1] if len(argv) > 1 else os.path.join(here, "..", "components")
    apps, problems = check(components)
    for p in problems:
        print("DEPENDENCY RULE:", p, file=sys.stderr)
    if problems:
        return 1
    print(f"{len(apps)} apps depend only on app_api, ui_kit and LVGL: {', '.join(apps)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
