// Reference harness: decode a defmt byte stream with the official defmt-decoder
// and print one JSON object per frame (or per decode error).
use defmt_decoder::{DecodeError, Table};
use std::io::Read;

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let elf = std::fs::read(&args[1])?;
    let mut input = Vec::new();
    std::fs::File::open(&args[2])?.read_to_end(&mut input)?;
    let table = Table::parse(&elf)?.expect("no defmt table");
    let mut stream = table.new_stream_decoder();
    stream.received(&input);
    loop {
        match stream.decode() {
            Ok(frame) => {
                let ts = frame.display_timestamp().map(|t| t.to_string());
                let obj = serde_json::json!({
                    "index": frame.index(),
                    "level": frame.level().map(|l| l.as_str()),
                    "timestamp": ts,
                    "message": frame.display_message().to_string(),
                    "line": frame.display(false).to_string(),
                });
                println!("{}", obj);
            }
            Err(DecodeError::UnexpectedEof) => break,
            Err(DecodeError::Malformed) => {
                println!("{}", serde_json::json!({"error": "malformed"}));
                if !table.encoding().can_recover() { break; }
            }
        }
    }
    Ok(())
}
