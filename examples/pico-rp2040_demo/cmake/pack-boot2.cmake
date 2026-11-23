file(SIZE "${INPUT}" code_size)
if(code_size LESS 1 OR code_size GREATER 252)
    message(FATAL_ERROR "Expected 1 to 252 bytes of second-stage code, got ${code_size}")
endif()

file(READ "${INPUT}" code HEX)
math(EXPR padding "252 - ${code_size}")
string(REPEAT "00" ${padding} zero_bytes)
string(APPEND code "${zero_bytes}")

# CRC-32/MPEG-2 over the code and its zero padding, as read by the boot ROM.
set(crc 0xFFFFFFFF)
foreach(offset RANGE 0 502 2)
    string(SUBSTRING "${code}" ${offset} 2 byte)
    math(EXPR crc "${crc} ^ (0x${byte} << 24)")
    foreach(bit RANGE 1 8)
        math(EXPR top "${crc} & 0x80000000")
        math(EXPR crc "(${crc} << 1) & 0xFFFFFFFF")
        if(top)
            math(EXPR crc "${crc} ^ 0x04C11DB7")
        endif()
    endforeach()
endforeach()

file(WRITE "${OUTPUT}"
    ".section .boot2, \"a\", %progbits\n.incbin \"${INPUT}\"\n.space ${padding}, 0\n.word ${crc}\n")
