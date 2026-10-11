#pragma once

#include <stdbool.h>
#include <string.h>

/* Registers every app, or with `only` just that one — the sim can run a single app with no
 * other app compiled into what it exercises. */
bool apps_register(const char *only);
