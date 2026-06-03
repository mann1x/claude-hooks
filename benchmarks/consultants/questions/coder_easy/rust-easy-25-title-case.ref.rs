use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let parts: Vec<String> = s.split_whitespace().map(|w| {
        let mut c = w.chars();
        let first = c.next().unwrap().to_ascii_uppercase();
        format!("{}{}", first, c.as_str().to_ascii_lowercase())
    }).collect();
    println!("{}", parts.join(" "));
}
