/*
 * An ESP32 image that runs from RAM only, loaded by the ROM from flash offset
 * 0x1000 the way it loads a second-stage bootloader.
 *
 * The two regions are the ESP-IDF v6.1 bootloader's own
 * (components/bootloader/subproject/main/ld/esp32/bootloader.memory.ld.in):
 * IRAM from 0x40080400, past the first 1 KiB where an application's exception
 * vectors go, and the DRAM above the ROM bootloader's stack. Code goes to IRAM;
 * strings and data go to DRAM, because IRAM only takes 32-bit accesses and
 * ets_printf reads its format byte by byte.
 */

MEMORY
{
  iram_seg (RWX) : org = 0x40080400, len = 0xfc00
  dram_seg (RW)  : org = 0x3FFF0000, len = 0x6000
}

ENTRY(call_start_cpu0)

/* components/esp_rom/esp32/ld/esp32.rom.ld */
PROVIDE(ets_printf = 0x40007d54);
PROVIDE(ets_delay_us = 0x40008534);

SECTIONS
{
  .iram0.text : ALIGN(4)
  {
    *(.literal .text .literal.* .text.*)
  } > iram_seg

  .dram0.data : ALIGN(4)
  {
    *(.rodata .rodata.* .data .data.*)
    *(.bss .bss.* COMMON)
  } > dram_seg
}
