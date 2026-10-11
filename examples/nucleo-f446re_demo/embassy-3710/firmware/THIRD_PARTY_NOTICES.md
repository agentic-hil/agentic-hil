# Third-party components in the images

The two images in this directory are Rust binaries of the crate in
[embassy/](../embassy/), built against the components below. Only that crate is
ours: `Cargo.toml`, `Cargo.lock`, `build.rs`, `src/lib.rs`, `src/bin/baseline.rs`
and `src/bin/patched.rs`, under Apache-2.0, like this repository.
[embassy-independent-outputs.patch](../embassy-independent-outputs.patch) is a
change to Embassy and is offered under Embassy's own terms, MIT OR Apache-2.0.

| Component | Where it comes from | Revision in these images | License |
|---|---|---|---|
| Embassy: `embassy-stm32` 0.6.0, `embassy-executor` 0.10.0, `embassy-time` 0.5.1 and the Embassy crates they pull in (`embassy-hal-internal` 0.5.0, `embassy-sync` 0.8.0, `embassy-futures` 0.1.2, `embassy-time-driver` 0.2.2, `embassy-time-queue-utils` 0.3.2, `embassy-executor-timer-queue` 0.1.1, `embassy-executor-macros` 0.8.0, `embassy-embedded-hal` 0.6.0, `embassy-crypto` 0.1.0, `embassy-usb-driver` 0.2.2, `embassy-usb-synopsys-otg` 0.4.0), as path dependencies of one checkout | [embassy-rs/embassy](https://github.com/embassy-rs/embassy) | `27d398592b8a8c415c54eb04a0893aa9ef20316d`; in `patched`, with the patch above applied to two files of `embassy-stm32` | MIT OR Apache-2.0 |
| `stm32-metapac` 21.0.0, the register definitions, and the `memory.x` the `memory-x` feature generates from them | [embassy-rs/stm32-data-generated](https://github.com/embassy-rs/stm32-data-generated) | `b99bf17d58e6a8d385faf4b87b5f310ca2c057a0`, tag `stm32-data-37a22f31552ba1fd29b3ef192c4578b84abee6e1`, the one the Embassy revision pins | MIT OR Apache-2.0 |
| `cortex-m` 0.7.9 and `cortex-m-rt` 0.7.7 (with their macro crates 0.7.8 and 0.7.7), including the `link.x` linker script | [rust-embedded/cortex-m](https://github.com/rust-embedded/cortex-m), from crates.io | 0.7.9 and 0.7.7 | MIT OR Apache-2.0 |
| `unitrait` 2.0.0 with `unitrait-macros` | [embassy-rs/unitrait](https://github.com/embassy-rs/unitrait) | `bb96601b3d293fa27d1ca2891f6ea148e534e433` | MIT OR Apache-2.0 |
| `xarxa-driver` 0.1.0 | [embassy-rs/xarxa](https://github.com/embassy-rs/xarxa) | `93228f1ec64720122016be52be4671e413fa92a9` | 0BSD |
| Rust's `core` and `compiler_builtins` libraries | rustc 1.97.0 (2d8144b78 2026-07-07), target `thumbv7em-none-eabihf` | the toolchain's own | MIT OR Apache-2.0; parts of `compiler_builtins` derived from LLVM's compiler-rt are Apache-2.0 WITH LLVM-exception |

The crates from crates.io in the images' dependency tree for the target, as
`cargo tree --target thumbv7em-none-eabihf -e normal` lists them for
[embassy/Cargo.lock](../embassy/Cargo.lock), each under the license its own
`Cargo.toml` declares:

| Crate | Version | License |
|---|---|---|
| `aligned` | 0.4.3 | MIT OR Apache-2.0 |
| `as-slice` | 0.2.1 | MIT OR Apache-2.0 |
| `bare-metal` | 0.2.5 | MIT OR Apache-2.0 |
| `bit_field` | 0.10.3 | Apache-2.0/MIT |
| `bitfield` | 0.13.2 | MIT OR Apache-2.0 |
| `bitflags` | 2.13.2 | MIT OR Apache-2.0 |
| `block-device-driver` | 0.2.0 | MIT |
| `byteorder` | 1.5.0 | Unlicense OR MIT |
| `cfg-if` | 1.0.5 | MIT OR Apache-2.0 |
| `cordyceps` | 0.3.5 | MIT |
| `critical-section` | 1.2.0 | MIT OR Apache-2.0 |
| `dsp-fixedpoint` | 0.2.2 | MIT OR Apache-2.0 |
| `embedded-can` | 0.4.1 | MIT OR Apache-2.0 |
| `embedded-hal` | 0.2.7 and 1.0.0 | MIT OR Apache-2.0 |
| `embedded-hal-async` | 1.0.0 | MIT OR Apache-2.0 |
| `embedded-hal-nb` | 1.0.0 | MIT OR Apache-2.0 |
| `embedded-io` | 0.6.1 and 0.7.1 | MIT OR Apache-2.0 |
| `embedded-io-async` | 0.6.1 and 0.7.0 | MIT OR Apache-2.0 |
| `embedded-storage` | 0.3.2 | MIT OR Apache-2.0 |
| `embedded-storage-async` | 0.4.2 | MIT OR Apache-2.0 |
| `futures-core`, `futures-sink`, `futures-task`, `futures-util` | 0.3.34 | MIT OR Apache-2.0 |
| `hash32` | 0.3.1 | MIT OR Apache-2.0 |
| `heapless` | 0.9.3 | MIT OR Apache-2.0 |
| `nb` | 0.1.3 and 1.1.0 | MIT OR Apache-2.0 |
| `num-traits` | 0.2.19 | MIT OR Apache-2.0 |
| `once_cell` | 1.21.4 | MIT OR Apache-2.0 |
| `pin-project-lite` | 0.2.17 | Apache-2.0 OR MIT |
| `portable-atomic` | 1.15.0 | Apache-2.0 OR MIT |
| `rand_core` | 0.6.4, 0.9.5 and 0.10.1 | MIT OR Apache-2.0 |
| `sdio` | 0.5.2 | MIT OR Apache-2.0 |
| `sdio-host` | 0.9.0 | MIT OR Apache-2.0 |
| `stable_deref_trait` | 1.2.1 | MIT OR Apache-2.0 |
| `static_assertions` | 1.1.0 | MIT OR Apache-2.0 |
| `stm32-fmc` | 0.4.0 | MIT/Apache-2.0 |
| `vcell` | 0.1.3 | MIT OR Apache-2.0 |
| `void` | 1.0.2 | MIT |
| `volatile-register` | 0.2.2 | MIT OR Apache-2.0 |

Procedural macros run on the build host and are not linked into the images;
the tree lists them and their own dependencies as well: `document-features`
0.2.12 and `litrs` 1.0.0 (MIT OR Apache-2.0), `trait-set` 0.3.0 (MIT),
`darling`, `darling_core` and `darling_macro` 0.20.11 (MIT), `ident_case` 1.0.1
(MIT/Apache-2.0), `strsim` 0.11.1 (MIT), `fnv` 1.0.7 (Apache-2.0 / MIT),
`proc-macro2` 1.0.107, `quote` 1.0.47 and `syn` 1.0.109, 2.0.119 and 3.0.7
(MIT OR Apache-2.0), and `unicode-ident` 1.0.26 ((MIT OR Apache-2.0) AND
Unicode-3.0). The remaining entries of `Cargo.lock` are build-script and
host-only dependencies and do not enter the images.

Each upstream crate carries its own copyright and license texts; the revisions
and versions above are where the exact texts can be read. Nothing in these
images is redistributed with its notices removed, and no third-party source is
modified except the two files of `embassy-stm32` that the patch changes in the
`patched` image, which is the change under test.
