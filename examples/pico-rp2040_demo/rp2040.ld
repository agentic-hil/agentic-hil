ENTRY(reset_handler)

MEMORY
{
    FLASH (rx) : ORIGIN = 0x10000000, LENGTH = 2M
    RAM (rwx)  : ORIGIN = 0x20000000, LENGTH = 264K
}

SECTIONS
{
    .boot2 ORIGIN(FLASH) : { KEEP(*(.boot2)) } > FLASH
    ASSERT(SIZEOF(.boot2) == 256, "RP2040 boot block must be 256 bytes")

    .vectors 0x10000100 :
    {
        LONG(ORIGIN(RAM) + LENGTH(RAM))
        LONG(reset_handler | 1)
    } > FLASH

    .text : { *(.text*) *(.rodata*) } > FLASH
}
