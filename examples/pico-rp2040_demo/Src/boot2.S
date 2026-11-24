.syntax unified
.thumb
.section .text.boot2, "ax", %progbits

.equ XIP_SSI_BASE,       0x18000000
.equ SSI_CTRLR0,         0x00
.equ SSI_CTRLR1,         0x04
.equ SSI_SSIENR,         0x08
.equ SSI_BAUDR,          0x14
.equ SSI_SPI_CTRLR0,     0xf4
.equ SSI_READ_32_BITS,   (31 << 16) | (3 << 8)
.equ SSI_READ_COMMAND,   (0x03 << 24) | (2 << 8) | (6 << 2)
.equ APP_VECTORS,        0x10000100
.equ VTOR,               0xe000ed08

// The ROM runs this block from 0x20041f00. Keep references to its literal
// pool PC-relative so the raw object section needs no linker relocations.
    ldr r0, =XIP_SSI_BASE
    movs r1, #0
    str r1, [r0, #SSI_SSIENR]
    str r1, [r0, #SSI_CTRLR1]       // One data frame per read.
    movs r1, #4
    str r1, [r0, #SSI_BAUDR]
    ldr r1, =SSI_READ_32_BITS       // Standard SPI, EEPROM-read mode.
    str r1, [r0, #SSI_CTRLR0]
    movs r2, #SSI_SPI_CTRLR0
    ldr r1, =SSI_READ_COMMAND       // 03h, 8-bit instruction, 24-bit address.
    str r1, [r0, r2]
    movs r1, #1
    str r1, [r0, #SSI_SSIENR]

    ldr r0, =APP_VECTORS
    ldr r1, =VTOR
    str r0, [r1]
    dsb
    isb
    ldmia r0!, {r1, r2}            // Initial stack pointer and reset vector.
    msr msp, r1
    bx r2
    .ltorg
