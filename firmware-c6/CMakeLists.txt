cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# The project name is also what `ota.c` compares an offered image against, so the box handing
# this board the S3 panels' `jbrain-endpoint` image is refused before a byte reaches a slot.
project(jbrain-jpanel-c6)
