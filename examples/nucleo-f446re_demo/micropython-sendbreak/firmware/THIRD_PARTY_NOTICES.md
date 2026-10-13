# Third-party components in the images

The two images in this directory are MicroPython for the `NUCLEO_F446RE` board,
built from MicroPython `23f0e61b47f0adf9a8f1259ec603892f27a69eb7` with the
components below. Only [micropython/main.py](../micropython/main.py), frozen into
both images as a string, is ours; it, the plans and the scripts are Apache-2.0,
like this repository. [uart-sendbreak-docs.patch](../uart-sendbreak-docs.patch)
is a change to MicroPython's documentation and is offered under MicroPython's
own terms, MIT. It touches no source file, so the code compiled into `patched`
is the same as in `baseline`.

What is listed here is what the linker put into the images: the input files
that contribute code or data to them, read from the linker map of each build.
Both builds link the same input files with the same sizes.

| Component | Where it comes from | Revision in these images | In the images | License |
|---|---|---|---|---|
| MicroPython: `py/`, `extmod/`, `shared/`, `drivers/` and the stm32 port in `ports/stm32/` | [micropython/micropython](https://github.com/micropython/micropython) | `23f0e61b47f0adf9a8f1259ec603892f27a69eb7` | the interpreter, the runtime and the port | MIT |
| `ports/stm32/system_stm32.c` and `ports/stm32/stm32_it.c` | MicroPython, adapted from STMicroelectronics' STM32Cube templates | the same | clock setup and exception handlers | MIT and BSD-3-Clause (STMicroelectronics) |
| `lib/libm` | MicroPython, from musl 0.9.15 and 1.1.16 and from newlib-nano-2 (fdlibm) | the same | the single-precision math functions | MIT (MicroPython, musl) and the Sun Microsystems fdlibm notice |
| `lib/oofatfs` | MicroPython's customised FatFs R0.13c by ChaN | the same | the FAT filesystem (`ff.c`, `ffunicode.c`) | the FatFs license (BSD-1-Clause) |
| `lib/re1.5` | re1.5 by Russ Cox and Paul Sokolovsky, compiled into `extmod/modre.c` | the same | the `re` module | BSD-3-Clause |
| `lib/uzlib`: `tinflate.c`, `header.c`, `adler32.c` and `crc32.c` | uzlib by Joergen Ibsen and Paul Sokolovsky, compiled into `extmod/moddeflate.c` | the same | decompression in the `deflate` module and the checksums | Zlib |
| STM32F4 HAL 1.7.1 (19 modules, among them `stm32f4xx_hal_uart.c`) and the STM32F4xx CMSIS device files 2.6.1 (`startup_stm32f446xx.s`, `system_stm32f4xx.c`, `stm32f446xx.h`) | [micropython/stm32lib](https://github.com/micropython/stm32lib), STMicroelectronics' STM32Cube with MicroPython's patches | `07619754261b571fb2f7c9f36ab94c203880d9fb` | peripheral drivers, the vector table and the reset code | BSD-3-Clause (STMicroelectronics) |
| CMSIS-Core(M) 6.1 headers (`core_cm4.h`, `cmsis_gcc.h` and the headers they include) | [ARM-software/CMSIS_6](https://github.com/ARM-software/CMSIS_6) | `45dab712ad84f8cbbf2b7bfc089c19088507df6f` | inline functions for the core registers | Apache-2.0 (Arm Limited) |
| `libgcc` (`_aeabi_ldivmod`, `_aeabi_uldivmod`, `_udivmoddi4`, `_arm_addsubdf3`, `_arm_truncdfsf2`, `_dvmd_tls`) | GNU Tools for STM32 14.3.rel1 (GCC 14.3.1) | the toolchain's own | 64-bit division and double-precision helpers | GPL-3.0-or-later WITH GCC-exception-3.1 |

The build also compiles TinyUSB, ST's USB device library in
`ports/stm32/usbdev` and the port's USB files, but the linker keeps none of
their code, as the board enables no USB device; no other library from `lib/` is
in the images. MicroPython's own frozen modules are not in them either: the
build froze only `main.py`. The libgcc functions are listed for completeness;
the GCC Runtime Library Exception permits distributing them as part of a
compiled program under terms of one's choice.

The notices of every component the images contain follow, copied from the files
named.

## MicroPython

From `LICENSE` at the MicroPython revision above. It also covers
`system_stm32.c`, `stm32_it.c` and MicroPython's own files in `lib/libm`,
whose headers carry `Copyright (c) 2013, 2014 Damien P. George`, and
`lib/oofatfs/ffconf.h` (`Copyright (c) 2013-2019 Damien P. George`).

```text
The MIT License (MIT)

Copyright (c) 2013-2026 Damien P. George

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

## STMicroelectronics

From `STM32F4xx_HAL_Driver/Src/stm32f4xx_hal_uart.c`. The other HAL modules and
`stm32f446xx.h` carry the same notice, `startup_stm32f446xx.s` and
`system_stm32f4xx.c` the same conditions and disclaimer under `COPYRIGHT 2017
STMicroelectronics`, and MicroPython's `system_stm32.c` and `stm32_it.c` under
`COPYRIGHT 2014 STMicroelectronics` and `COPYRIGHT(c) 2014 STMicroelectronics`.

```text
© COPYRIGHT(c) 2017 STMicroelectronics

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:
  1. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.
  2. Redistributions in binary form must reproduce the above copyright notice,
     this list of conditions and the following disclaimer in the documentation
     and/or other materials provided with the distribution.
  3. Neither the name of STMicroelectronics nor the names of its contributors
     may be used to endorse or promote products derived from this software
     without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Arm CMSIS-Core

From `CMSIS/Core/Include/core_cm4.h`; `cmsis_compiler.h`, `cmsis_gcc.h` and
`m-profile/cmsis_gcc_m.h` carry `Copyright (c) 2009-2023 Arm Limited`,
`cmsis_version.h` `Copyright (c) 2009-2023 ARM Limited` and
`m-profile/armv7m_mpu.h` `Copyright (c) 2017-2020 Arm Limited`, under the same
license. The text of the Apache License 2.0 is this
repository's [LICENSE](../../../../LICENSE). CMSIS_6 has no NOTICE file.

```text
Copyright (c) 2009-2024 Arm Limited. All rights reserved.

SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the License); you may
not use this file except in compliance with the License.
You may obtain a copy of the License at

www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an AS IS BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

## musl

The `lib/libm` functions marked as taken from musl 0.9.15 (`acoshf`, `asinf`,
`acosf`, `asinhf`, `atan2f`, `atanf`, `atanhf`, `fmodf`, `log1pf` and parts of
`libm.h`) and from musl 1.1.16 (`nearbyintf`, from `rintf`). The tree carries
no copy of musl's license; this is the MIT text from musl's `COPYRIGHT` at
v1.1.16, which is the same at v0.9.15 except that its copyright line reads
`Copyright © 2005-2013 Rich Felker`.

```text
Copyright © 2005-2014 Rich Felker, et al.

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## Sun Microsystems (fdlibm)

From `lib/libm/kf_rem_pio2.c`; the same notice is in every `lib/libm` file
taken from newlib-nano-2 and in the musl-derived ones that carry it.

```text
Copyright (C) 1993 by Sun Microsystems, Inc. All rights reserved.

Developed at SunPro, a Sun Microsystems, Inc. business.
Permission to use, copy, modify, and distribute this
software is freely granted, provided that this notice
is preserved.
```

## FatFs

From `lib/oofatfs/ff.c`; `ffunicode.c` and `ff.h` carry the same notice.

```text
FatFs - Generic FAT Filesystem Module  R0.13c

Copyright (C) 2018, ChaN, all right reserved.

FatFs module is an open source software. Redistribution and use of FatFs in
source and binary forms, with or without modification, are permitted provided
that the following condition is met:

1. Redistributions of source code must retain the above copyright notice,
   this condition and the following disclaimer.

This software is provided by the copyright holder and contributors "AS IS"
and any warranties related to this software are DISCLAIMED.
The copyright owner or contributors be NOT LIABLE for any damages caused
by use of this software.
```

## re1.5

The files in `lib/re1.5` carry `Copyright 2007-2009 Russ Cox` and `Copyright
2014 Paul Sokolovsky` and refer to a LICENSE file that the MicroPython tree does
not include. This is that file, from
[pfalcon/re1.5](https://github.com/pfalcon/re1.5/blob/master/LICENSE):

```text
Copyright (c) 2007-2009 Russ Cox, Google Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google, Inc. nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## uzlib

From `lib/uzlib/tinflate.c`; `header.c` and `uzlib.h` carry the same notice,
`adler32.c` and `crc32.c` the same terms with `Copyright (c) 2003 by Joergen
Ibsen / Jibz` and `Copyright (c) 1998-2003 by Joergen Ibsen / Jibz`.

```text
uzlib  -  tiny deflate/inflate library (deflate, gzip, zlib)

Copyright (c) 2003 by Joergen Ibsen / Jibz
All Rights Reserved
http://www.ibsensoftware.com/

Copyright (c) 2014-2018 by Paul Sokolovsky

Optimised for MicroPython:
Copyright (c) 2023 by Jim Mussared

This software is provided 'as-is', without any express
or implied warranty.  In no event will the authors be
held liable for any damages arising from the use of
this software.

Permission is granted to anyone to use this software
for any purpose, including commercial applications,
and to alter it and redistribute it freely, subject to
the following restrictions:

1. The origin of this software must not be
   misrepresented; you must not claim that you
   wrote the original software. If you use this
   software in a product, an acknowledgment in
   the product documentation would be appreciated
   but is not required.

2. Altered source versions must be plainly marked
   as such, and must not be misrepresented as
   being the original software.

3. This notice may not be removed or altered from
   any source distribution.
```

`adler32.c` and `crc32.c` also say where their algorithms come from:

```text
Adler-32 algorithm taken from the zlib source, which is
Copyright (C) 1995-1998 Jean-loup Gailly and Mark Adler

CRC32 algorithm taken from the zlib source, which is
Copyright (C) 1995-1998 Jean-loup Gailly and Mark Adler
```
