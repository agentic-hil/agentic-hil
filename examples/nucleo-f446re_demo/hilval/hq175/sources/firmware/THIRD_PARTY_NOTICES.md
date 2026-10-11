# Third-party components in the hq175 images

The two images in this directory are built from the sources here plus the
components below. Each keeps its own licence; none of them is modified except
where this directory's patch says so.

| Component | Upstream | Revision or version | Licence |
|---|---|---|---|
| Embassy (`embassy-stm32`, `embassy-executor`, `embassy-time` and the crates they depend on) | [embassy-rs/embassy](https://github.com/embassy-rs/embassy) | `27d398592b8a8c415c54eb04a0893aa9ef20316d` | MIT OR Apache-2.0 |
| `stm32-metapac` (the register definitions) | [embassy-rs/stm32-data-generated](https://github.com/embassy-rs/stm32-data-generated) | `b99bf17d58e6a8d385faf4b87b5f310ca2c057a0` | MIT OR Apache-2.0 |
| `cortex-m`, `cortex-m-rt` | [rust-embedded/cortex-m](https://github.com/rust-embedded/cortex-m) | 0.7.x | MIT OR Apache-2.0 |
| Further crates Cargo resolves for the above (`critical-section`, `embedded-hal`, `defmt` traits, ...) | their own repositories | as Cargo resolved them offline | MIT OR Apache-2.0 |

`embassy-independent-outputs.patch` changes two files of `embassy-stm32`. It is a
change to Embassy, offered under Embassy's own terms (MIT OR Apache-2.0), and the
copies of those licences are in the Embassy checkout it applies to
(`LICENSE-MIT`, `LICENSE-APACHE`), also copied into this repository at
`reference/hq175/embassy/`.

Everything else in this directory (the crate in `embassy/`, this file and the
README) is part of hil-validations and carries that repository's licence.
