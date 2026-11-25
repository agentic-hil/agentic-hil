/* STM32H533RE: 512 KB flash at 0x08000000, the first 128 KB of SRAM at 0x20000000. */
ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 512K
    RAM (rw) : ORIGIN = 0x20000000, LENGTH = 128K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
    .vectors : { KEEP(*(.vectors)) } > FLASH
    .text : { *(.text*) *(.rodata .rodata.str* .rodata.cst*) KEEP(*(.rodata.payload)) . = ALIGN(4); } > FLASH
    .bss (NOLOAD) : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
    /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.ARM.attributes) }
}
ASSERT(_sbss == _ebss || SIZEOF(.bss) < LENGTH(RAM), "bss does not fit")