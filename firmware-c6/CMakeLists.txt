cmake_minimum_required(VERSION 3.16)
# THE APP FRAMEWORK IS NOT IN THE DEVICE IMAGE YET. ESP-IDF builds every directory under
# components/ it finds, and those need LVGL and drivers this image does not carry, so the
# bring-up image (and the committed dist/ bytes) stays exactly what main/ builds. C2 deletes
# these two lines, adds LVGL as a managed component and has main/ require the shell
# (README "Layout").
file(GLOB _framework RELATIVE ${CMAKE_CURRENT_LIST_DIR}/components ${CMAKE_CURRENT_LIST_DIR}/components/*)
set(EXCLUDE_COMPONENTS ${_framework})
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# The project name is also what `ota.c` compares an offered image against, so the box handing
# this board the S3 panels' `jbrain-endpoint` image is refused before a byte reaches a slot.
project(jbrain-jpanel-c6)
