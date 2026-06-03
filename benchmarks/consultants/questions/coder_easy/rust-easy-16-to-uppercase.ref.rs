use std::io::{self, BufRead};
fn main() {
    let mut s = String::new();
    io::stdin().lock().read_line(&mut s).unwrap();
    let s = s.trim_end_matches(['\n', '\r']);
    println!("{}", s.to_ascii_uppercase());
}
