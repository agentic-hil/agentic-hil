# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(hq179)

# The Pulse IO driver under test is compiled from the copy named on the command
# line (-DHQ179_DRIVER_SRC=...), unpatched or patched; prj.conf keeps the
# in-tree copy out of the image.
if(NOT DEFINED HQ179_DRIVER_SRC)
  message(FATAL_ERROR "HQ179_DRIVER_SRC must name the copy of pulse_io_stm32_timer_dma.c to compile")
endif()

target_sources(app PRIVATE src/main.c src/sampler.c ${HQ179_DRIVER_SRC})
