# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(zephyr_120620_f446re)

set(Z120620_VARIANT "unset" CACHE STRING "Image variant reported on the console")
set(Z120620_ZEPHYR "unset" CACHE STRING "Zephyr commit reported on the console")

target_sources(app PRIVATE src/main.c)
target_compile_definitions(app PRIVATE
  Z120620_VARIANT="${Z120620_VARIANT}"
  Z120620_ZEPHYR="${Z120620_ZEPHYR}"
)

# Observe the Stop mode exit path without changing it: the clock restore
# called from pm_state_exit_post_ops() and the RTC alarm's EXTI clear.
zephyr_ld_options(
  -Wl,--wrap=stm32_clock_control_init
  -Wl,--wrap=stm32_exti_clear_pending
)
