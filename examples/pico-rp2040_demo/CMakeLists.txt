cmake_minimum_required(VERSION 3.24)
project(pico-rp2040_demo LANGUAGES C ASM)

add_library(pico_boot2 OBJECT Src/boot2.S)
set(boot2_bin "${CMAKE_CURRENT_BINARY_DIR}/boot2.bin")
set(boot2_source "${CMAKE_CURRENT_BINARY_DIR}/boot2_image.S")
set(boot2_script "${CMAKE_CURRENT_SOURCE_DIR}/cmake/pack-boot2.cmake")
add_custom_command(
    OUTPUT "${boot2_source}"
    BYPRODUCTS "${boot2_bin}"
    COMMAND "${CMAKE_OBJCOPY}" -O binary -j .text.boot2 $<TARGET_OBJECTS:pico_boot2> "${boot2_bin}"
    COMMAND "${CMAKE_COMMAND}" "-DINPUT=${boot2_bin}" "-DOUTPUT=${boot2_source}" -P "${boot2_script}"
    DEPENDS $<TARGET_OBJECTS:pico_boot2> "${boot2_script}"
    VERBATIM
)

set(linker_script "${CMAKE_CURRENT_SOURCE_DIR}/rp2040.ld")
add_executable(${PROJECT_NAME} Src/main.c "${boot2_source}")
set_target_properties(${PROJECT_NAME} PROPERTIES SUFFIX ".elf" LINK_DEPENDS "${linker_script}")
target_compile_options(${PROJECT_NAME} PRIVATE -ffreestanding)
target_link_options(${PROJECT_NAME} PRIVATE -nostdlib "-T${linker_script}")
