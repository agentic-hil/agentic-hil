// Build script of hq184-fw.
//
// It links against memory.x, and it turns the two frozen campaign inputs (../cases.json and
// ../measurement.json) into OUT_DIR/frozen.rs, so the firmware runs exactly the cases the
// analyzer expects. It uses the standard library only: a small JSON reader for the frozen
// shape and a SHA-256 for the digests the firmware prints in its BOOT line.

use std::env;
use std::fmt::Write as _;
use std::fs;
use std::path::PathBuf;
use std::process::Command;

#[allow(dead_code)]
#[derive(Debug, Clone)]
enum Json {
    Null,
    Bool(bool),
    Num(String),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    fn get(&self, key: &str) -> &Json {
        match self {
            Json::Obj(items) => items
                .iter()
                .find(|(k, _)| k == key)
                .map(|(_, v)| v)
                .unwrap_or_else(|| panic!("missing key {key}")),
            _ => panic!("not an object while reading {key}"),
        }
    }
    fn int(&self) -> u64 {
        match self {
            Json::Num(text) => text.parse().unwrap_or_else(|_| panic!("not an integer: {text}")),
            _ => panic!("not a number"),
        }
    }
    fn str(&self) -> &str {
        match self {
            Json::Str(text) => text,
            _ => panic!("not a string"),
        }
    }
    fn arr(&self) -> &[Json] {
        match self {
            Json::Arr(items) => items,
            _ => panic!("not an array"),
        }
    }
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && self.s[self.i].is_ascii_whitespace() {
            self.i += 1;
        }
    }
    fn string(&mut self) -> String {
        assert_eq!(self.s[self.i], b'"', "expected a string");
        self.i += 1;
        let mut out = String::new();
        while self.s[self.i] != b'"' {
            if self.s[self.i] == b'\\' {
                self.i += 1;
                out.push(match self.s[self.i] {
                    b'n' => '\n',
                    b't' => '\t',
                    other => other as char,
                });
            } else {
                out.push(self.s[self.i] as char);
            }
            self.i += 1;
        }
        self.i += 1;
        out
    }
    fn value(&mut self) -> Json {
        self.ws();
        match self.s[self.i] {
            b'{' => {
                self.i += 1;
                let mut items = Vec::new();
                loop {
                    self.ws();
                    match self.s[self.i] {
                        b'}' => {
                            self.i += 1;
                            break;
                        }
                        b',' => self.i += 1,
                        _ => {
                            let key = self.string();
                            self.ws();
                            assert_eq!(self.s[self.i], b':', "expected a colon");
                            self.i += 1;
                            let value = self.value();
                            items.push((key, value));
                        }
                    }
                }
                Json::Obj(items)
            }
            b'[' => {
                self.i += 1;
                let mut items = Vec::new();
                loop {
                    self.ws();
                    match self.s[self.i] {
                        b']' => {
                            self.i += 1;
                            break;
                        }
                        b',' => self.i += 1,
                        _ => items.push(self.value()),
                    }
                }
                Json::Arr(items)
            }
            b'"' => Json::Str(self.string()),
            b't' => {
                self.i += 4;
                Json::Bool(true)
            }
            b'f' => {
                self.i += 5;
                Json::Bool(false)
            }
            b'n' => {
                self.i += 4;
                Json::Null
            }
            _ => {
                let start = self.i;
                while self.i < self.s.len() && b"+-0123456789.eE".contains(&self.s[self.i]) {
                    self.i += 1;
                }
                assert!(self.i > start, "unexpected character in JSON");
                Json::Num(String::from_utf8(self.s[start..self.i].to_vec()).unwrap())
            }
        }
    }
}

fn parse(bytes: &[u8]) -> Json {
    let mut parser = Parser { s: bytes, i: 0 };
    parser.value()
}

fn sha256(data: &[u8]) -> [u8; 32] {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&((data.len() as u64) * 8).to_be_bytes());
    for block in message.chunks(64) {
        let mut w = [0u32; 64];
        for (i, word) in block.chunks(4).enumerate() {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let mut v = h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7].wrapping_add(s1).wrapping_add(ch).wrapping_add(K[i]).wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v[7] = v[6];
            v[6] = v[5];
            v[5] = v[4];
            v[4] = v[3].wrapping_add(t1);
            v[3] = v[2];
            v[2] = v[1];
            v[1] = v[0];
            v[0] = t1.wrapping_add(t2);
        }
        for i in 0..8 {
            h[i] = h[i].wrapping_add(v[i]);
        }
    }
    let mut out = [0u8; 32];
    for (i, word) in h.iter().enumerate() {
        out[4 * i..4 * i + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

/// The bytes with every CRLF turned into LF.
fn lf(bytes: Vec<u8>) -> Vec<u8> {
    let mut out = Vec::with_capacity(bytes.len());
    for (i, &byte) in bytes.iter().enumerate() {
        if byte == b'\r' && bytes.get(i + 1) == Some(&b'\n') {
            continue;
        }
        out.push(byte);
    }
    out
}

fn hex(bytes: &[u8]) -> String {
    let mut out = String::new();
    for byte in bytes {
        write!(out, "{byte:02x}").unwrap();
    }
    out
}

fn case_index(cases: &[Json], id: &str) -> usize {
    cases
        .iter()
        .position(|case| case.get("id").str() == id)
        .unwrap_or_else(|| panic!("unknown case id {id}"))
}

fn index_list(cases: &[Json], names: &Json) -> String {
    let mut out = String::from("&[");
    for name in names.arr() {
        write!(out, "{}, ", case_index(cases, name.str())).unwrap();
    }
    out.push(']');
    out
}

/// The embassy-stm32 commit as locked in Cargo.lock, or "unknown" before the lock exists.
fn locked_embassy_rev(lock: &str) -> String {
    let mut rest = lock;
    while let Some(at) = rest.find("name = \"embassy-stm32\"") {
        rest = &rest[at..];
        if let Some(source) = rest.find("source = \"") {
            let line = &rest[source..];
            let end = line.find('\n').unwrap_or(line.len());
            if let Some(hash) = line[..end].rfind('#') {
                // A CRLF checkout leaves the carriage return before the newline.
                return line[hash + 1..end].trim_end().trim_end_matches('"').to_string();
            }
        }
        rest = &rest[1..];
    }
    String::from("unknown")
}

/// HQ #356: the `[package.metadata.hq184]` table of a variant manifest (key, string value),
/// or None for the firmware's own manifest, which has no such table.
fn variant_metadata(manifest: &str) -> Option<Vec<(String, String)>> {
    let mut found = false;
    let mut inside = false;
    let mut items = Vec::new();
    for line in manifest.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            inside = line == "[package.metadata.hq184]";
            found |= inside;
        } else if inside {
            if let Some((key, value)) = line.split_once('=') {
                let value = value.trim();
                if value.len() >= 2 && value.starts_with('"') && value.ends_with('"') {
                    items.push((key.trim().to_string(), value[1..value.len() - 1].to_string()));
                }
            }
        }
    }
    found.then_some(items)
}

fn metadata_value<'a>(items: &'a [(String, String)], key: &str) -> &'a str {
    items
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
        .unwrap_or_else(|| panic!("[package.metadata.hq184] has no {key}"))
}

fn main() {
    let out = PathBuf::from(env::var("OUT_DIR").unwrap());

    // The firmware directory, which holds memory.x and sits next to the frozen inputs. For the
    // firmware's own manifest it is the manifest directory. A variant manifest (HQ #356,
    // variants/after) names it in [package.metadata.hq184], together with the base Embassy
    // revision and the patch applied to it; its own Cargo.lock patches Embassy to a path.
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let manifest = fs::read_to_string(manifest_dir.join("Cargo.toml")).unwrap_or_default();
    let variant = variant_metadata(&manifest);
    let firmware = match &variant {
        Some(items) => manifest_dir.join(metadata_value(items, "firmware_dir")),
        None => manifest_dir.clone(),
    };
    let lock_path = manifest_dir.join("Cargo.lock");
    let cases_path = firmware.join("../cases.json");
    let measurement_path = firmware.join("../measurement.json");

    // Link arguments: the memory map and the cortex-m-rt linker script.
    fs::copy(firmware.join("memory.x"), out.join("memory.x")).unwrap();
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rustc-link-arg-bins=-Tlink.x");
    let mut watched = vec![
        firmware.join("build.rs"),
        firmware.join("memory.x"),
        lock_path.clone(),
        cases_path.clone(),
        measurement_path.clone(),
    ];

    // The Embassy revision of the BOOT record. A variant names its base revision and the
    // first 16 hex digits of the SHA-256 of its patch (LF bytes): "<rev>+patch.<16 hex>". This
    // binds the image to the recorded patch, not to the bytes of the checkout. Without the
    // patch file the revision says "+patch.unrecorded", which no evaluation accepts.
    let variant_rev = variant.as_ref().map(|items| {
        let patch_path = manifest_dir.join(metadata_value(items, "patch"));
        watched.push(manifest_dir.join("Cargo.toml"));
        watched.push(patch_path.clone());
        let digest = match fs::read(&patch_path) {
            Ok(bytes) => hex(&sha256(&lf(bytes)))[..16].to_string(),
            Err(_) => {
                println!("cargo:warning=no patch at {}: the image is no evidence", patch_path.display());
                String::from("unrecorded")
            }
        };
        format!("{}+patch.{digest}", metadata_value(items, "embassy_rev"))
    });
    for path in &watched {
        println!("cargo:rerun-if-changed={}", path.display());
    }

    // The digests name the committed bytes (LF), whatever line endings the checkout has.
    let cases_bytes = lf(fs::read(&cases_path).unwrap());
    let measurement_bytes = lf(fs::read(&measurement_path).unwrap());
    let cases_doc = parse(&cases_bytes);
    let measurement = parse(&measurement_bytes);
    let cases = cases_doc.get("cases").arr();

    let rustc = env::var("RUSTC").unwrap_or_else(|_| String::from("rustc"));
    let version = Command::new(rustc)
        .arg("-V")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().replace(' ', "_"))
        .unwrap_or_else(|_| String::from("unknown"));
    let rev = match variant_rev {
        Some(rev) => rev,
        None => locked_embassy_rev(&fs::read_to_string(&lock_path).unwrap_or_default()),
    };
    let cases_sha = hex(&sha256(&cases_bytes));
    let measurement_sha = hex(&sha256(&measurement_bytes));
    let build_hash = sha256(format!("{cases_sha}{measurement_sha}{version}{rev}").as_bytes());
    let build_id = hex(&build_hash[..4]);
    let build_id_u32 = u32::from_be_bytes([build_hash[0], build_hash[1], build_hash[2], build_hash[3]]);

    let sampler = measurement.get("sampler");
    let mut src = String::new();
    writeln!(src, "// Generated by build.rs from ../cases.json and ../measurement.json.").unwrap();
    writeln!(src, "pub const K_HZ: u32 = {};", measurement.get("clock").get("spi_kernel_hz").int()).unwrap();
    writeln!(src, "pub const TRANSFER_LEN: usize = {};", measurement.get("transfer_length_bytes").int()).unwrap();
    writeln!(src, "pub const TRANSFERS: u32 = {};", measurement.get("transfers_per_occurrence").int()).unwrap();
    writeln!(src, "pub const PRNG_SEED: u32 = {};", measurement.get("prng_seed").int()).unwrap();
    writeln!(src, "pub const BAUD: u32 = {};", measurement.get("uart").get("baud").int()).unwrap();
    writeln!(src, "pub const WINDOW: usize = {};", sampler.get("window_samples").int()).unwrap();
    writeln!(src, "pub const GUARD: u32 = {};", sampler.get("guard_samples").int()).unwrap();
    writeln!(src, "pub const OCC_PER_BLOCK: u32 = {};", sampler.get("occurrences_per_block").int()).unwrap();
    writeln!(src, "pub const CASES_SHA256: &str = \"{cases_sha}\";").unwrap();
    writeln!(src, "pub const MEASUREMENT_SHA256: &str = \"{measurement_sha}\";").unwrap();
    writeln!(src, "pub const RUSTC: &str = \"{version}\";").unwrap();
    writeln!(src, "pub const EMBASSY_REV: &str = \"{rev}\";").unwrap();
    writeln!(src, "pub const BUILD_ID: &str = \"{build_id}\";").unwrap();
    writeln!(src, "pub const BUILD_ID_U32: u32 = {build_id_u32};").unwrap();
    writeln!(src, "pub struct Case {{ pub id: &'static str, pub f_req: u32 }}").unwrap();
    writeln!(src, "pub static CASES: [Case; {}] = [", cases.len()).unwrap();
    for case in cases {
        writeln!(src, "    Case {{ id: \"{}\", f_req: {} }},", case.get("id").str(), case.get("f_req_hz").int()).unwrap();
    }
    writeln!(src, "];").unwrap();
    let start = case_index(cases, cases_doc.get("start_case").str());
    let recovery = case_index(cases, cases_doc.get("recovery_case").str());
    writeln!(src, "pub const START_CASE: usize = {start};").unwrap();
    writeln!(src, "pub const RECOVERY_CASE: usize = {recovery};").unwrap();
    let sequences = cases_doc.get("sequences");
    for (name, constant) in [
        ("construct", "CONSTRUCT"),
        ("set_config_ascending", "SET_CONFIG_ASCENDING"),
        ("set_config_descending", "SET_CONFIG_DESCENDING"),
        ("set_config_revisited", "SET_CONFIG_REVISITED"),
    ] {
        writeln!(src, "pub static {constant}: &[usize] = {};", index_list(cases, sequences.get(name))).unwrap();
    }
    writeln!(
        src,
        "pub struct Capture {{ pub id: &'static str, pub psc: u32, pub arr: u32, pub idr_byte: u32, \
         pub pwm_steps: &'static [(u32, u32)] }}"
    )
    .unwrap();
    let captures = measurement.get("capture_configurations").arr();
    writeln!(src, "pub static CAPTURES: [Capture; {}] = [", captures.len()).unwrap();
    for capture in captures {
        let mut steps = String::from("&[");
        for step in capture.get("pwm_steps").arr() {
            write!(steps, "({}, {}), ", step.get("psc").int(), step.get("arr").int()).unwrap();
        }
        steps.push(']');
        writeln!(
            src,
            "    Capture {{ id: \"{}\", psc: {}, arr: {}, idr_byte: {}, pwm_steps: {steps} }},",
            capture.get("id").str(),
            capture.get("psc").int(),
            capture.get("arr").int(),
            capture.get("idr_byte").int(),
        )
        .unwrap();
    }
    writeln!(src, "];").unwrap();

    // HQ #340: the TIM8_CH2 capture path and its TIM2_CH3 reference configurations.
    let capture_path = measurement.get("capture_path");
    writeln!(src, "pub const CAPTURE_PATH_CAPTURES: usize = {};", capture_path.get("captures").int()).unwrap();
    writeln!(src, "pub struct CapturePathConfig {{ pub id: &'static str, pub pwm_psc: u32, pub pwm_arr: u32 }}").unwrap();
    let path_configs = capture_path.get("configurations").arr();
    writeln!(src, "pub static CAPTURE_PATH: [CapturePathConfig; {}] = [", path_configs.len()).unwrap();
    for config in path_configs {
        writeln!(
            src,
            "    CapturePathConfig {{ id: \"{}\", pwm_psc: {}, pwm_arr: {} }},",
            config.get("id").str(),
            config.get("pwm_psc").int(),
            config.get("pwm_arr").int(),
        )
        .unwrap();
    }
    writeln!(src, "];").unwrap();
    fs::write(out.join("frozen.rs"), src).unwrap();
}
