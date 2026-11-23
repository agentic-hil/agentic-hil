set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR cortex-m0plus)
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(CMAKE_C_COMPILER arm-none-eabi-gcc)
set(CMAKE_ASM_COMPILER arm-none-eabi-gcc)
set(CMAKE_C_FLAGS_INIT "-mcpu=cortex-m0plus -mthumb")
set(CMAKE_ASM_FLAGS_INIT "-mcpu=cortex-m0plus -mthumb")
