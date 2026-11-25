/* RAM regions from ESP-IDF v6.1's ESP32-S3 bootloader.memory.ld.in. */
MEMORY
{
  iram_seg (RWX) : org = 0x403C8700, len = 0x3000
  dram_seg (RW)  : org = 0x3FCE2700, len = 0x5000
}

ENTRY(call_start_cpu0)

/* ESP-IDF v6.1 components/esp_rom/esp32s3/ld/esp32s3.rom.ld. */
PROVIDE(ets_printf = 0x400005d0);
PROVIDE(ets_delay_us = 0x40000600);

SECTIONS
{
  .iram0.text : ALIGN(4)
  {
    *(.literal .literal.* .text .text.*)
  } > iram_seg

  .dram0.data : ALIGN(4)
  {
    *(.rodata .rodata.* .data .data.*)
  } > dram_seg
}
