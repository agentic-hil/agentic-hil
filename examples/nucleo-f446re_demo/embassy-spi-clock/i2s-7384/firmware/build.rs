// Links against cortex-m-rt's link.x; memory.x comes from embassy-stm32's `memory-x` feature.
fn main() {
    println!("cargo:rustc-link-arg-bins=--nmagic");
    println!("cargo:rustc-link-arg-bins=-Tlink.x");
    println!("cargo:rerun-if-env-changed=I2S7384_EMBASSY");
}
