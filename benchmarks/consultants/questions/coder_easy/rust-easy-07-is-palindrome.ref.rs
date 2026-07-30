use std::io::{self, BufRead};
fn main() {
    let mut s = String::new();
    io::stdin().lock().read_line(&mut s).unwrap();
    let s = s.trim_end_matches(['\n', '\r']);
    let rev: String = s.chars().rev().collect();
    println!("{}", if s == rev { "yes" } else { "no" });
}
