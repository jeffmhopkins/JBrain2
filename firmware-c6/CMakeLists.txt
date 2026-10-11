cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# lv_port/ holds the header LVGL's assert handler comes from (lv_assert_trap.h says why).
idf_build_set_property(INCLUDE_DIRECTORIES ${CMAKE_CURRENT_LIST_DIR}/lv_port APPEND)
# The project name is also what `ota.c` compares an offered image against, so the box handing
# this board the S3 panels' `jbrain-endpoint` image is refused before a byte reaches a slot.
project(jbrain-jpanel-c6)
