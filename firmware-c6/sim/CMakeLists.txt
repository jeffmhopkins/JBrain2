# The panel on the host: ui_kit, the shell, every app and the fake services, against the same
# LVGL release the device will pin, rendering headless to PNG through the same stripe codec
# the device's screenshot command uses.
cmake_minimum_required(VERSION 3.16)
project(jpanel_sim C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_EXTENSIONS ON)

include(FetchContent)
set(LV_CONF_PATH ${CMAKE_CURRENT_SOURCE_DIR}/lv_conf.h CACHE STRING "" FORCE)
set(LV_CONF_BUILD_DISABLE_EXAMPLES ON CACHE BOOL "" FORCE)
set(LV_CONF_BUILD_DISABLE_DEMOS ON CACHE BOOL "" FORCE)
set(LV_CONF_BUILD_DISABLE_THORVG_INTERNAL ON CACHE BOOL "" FORCE)
# An exact release. C2 pins the same one for the device (idf_component.yml), so the sim never
# renders with a different LVGL than the glass.
FetchContent_Declare(lvgl
    GIT_REPOSITORY https://github.com/lvgl/lvgl.git
    GIT_TAG v9.2.2
    GIT_SHALLOW TRUE)
FetchContent_MakeAvailable(lvgl)

set(C ${CMAKE_CURRENT_SOURCE_DIR}/../components)
file(GLOB FW_SRC ${C}/*/*.c)
file(GLOB FW_INC LIST_DIRECTORIES true ${C}/*/include)

# The device's app set (main/apps.c), so the sim registers exactly the apps the glass shows.
set(M ${CMAKE_CURRENT_SOURCE_DIR}/../main)
add_executable(jpanel-sim sim_main.c sim_png.c ${M}/apps.c ${FW_SRC})
target_include_directories(jpanel-sim PRIVATE ${FW_INC} ${M} ${C}/app_clock ${C}/app_settings
                                              ${C}/app_thread ${C}/app_jerv)
target_compile_options(jpanel-sim PRIVATE -Wall -Wextra -Werror)
target_link_libraries(jpanel-sim PRIVATE lvgl m)
