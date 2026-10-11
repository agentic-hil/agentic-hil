/* STM32F446RE: 512 KiB flash, 128 KiB SRAM; libopencm3's generic Cortex-M script does the rest. */
MEMORY
{
	rom (rx) : ORIGIN = 0x08000000, LENGTH = 512K
	ram (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

INCLUDE cortex-m-generic.ld
