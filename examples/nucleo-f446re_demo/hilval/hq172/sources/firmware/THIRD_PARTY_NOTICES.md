# Third-party components in the hq172 images

The two images are Zephyr applications: almost all of their code is upstream,
and each component keeps its own license. Only `zephyr/src/main.c`,
`zephyr/CMakeLists.txt`, `zephyr/prj.conf` and `zephyr/app.overlay` in this
directory are ours, under Apache-2.0.

| Component | Where it comes from | Revision in these images | License |
|---|---|---|---|
| Zephyr RTOS (kernel, device-PM subsystem, `drivers/serial/uart_stm32.c`, board and SoC devicetree for `nucleo_f446re`) | [zephyrproject-rtos/zephyr](https://github.com/zephyrproject-rtos/zephyr) | `90ccb4f9d4efc6a2886a8c910f7129856667ae04` in `head`, `3c61a12bdd8b2975b9180abcb80779ad3724a40d` in `base` (the head and the base of pull request #121031) | Apache-2.0 |
| hal_stm32: the STM32Cube HAL/LL drivers for the STM32F4 (`stm32f4xx_ll_usart.h` and the CMSIS device headers the driver uses), and the F446 pin control devicetree files | [zephyrproject-rtos/hal_stm32](https://github.com/zephyrproject-rtos/hal_stm32) | `c9550199186236d610092f4005f8c9bae2a6bfcf` | the STM32Cube parts are STMicroelectronics' BSD-3-Clause (see the `LICENSE` file beside them in that repository); the devicetree files added by the module are Apache-2.0 |
| CMSIS core headers for Cortex-M | [zephyrproject-rtos/cmsis_6](https://github.com/zephyrproject-rtos/cmsis_6) and [zephyrproject-rtos/cmsis](https://github.com/zephyrproject-rtos/cmsis) (Arm CMSIS) | `1c1840af7a7e757d6e2fec3ddb0e5ce0dfcc93c8` and `512cc7e895e8491696b61f7ba8066b4a182569b8` | Apache-2.0 |

Each upstream file carries its own copyright and license header; the revisions
above are where the exact texts can be read. Nothing in these images is
redistributed with its notices removed, and no third-party source is modified:
the change under test is the content of the `head` tree itself, so no patch is
applied by the build.
