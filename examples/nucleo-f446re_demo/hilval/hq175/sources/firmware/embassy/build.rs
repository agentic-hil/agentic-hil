// cargo_image sets the rustflags itself, so the linker arguments come from here.
fn main() {
    println!("cargo:rustc-link-arg-bins=--nmagic");
    println!("cargo:rustc-link-arg-bins=-Tlink.x");
}
