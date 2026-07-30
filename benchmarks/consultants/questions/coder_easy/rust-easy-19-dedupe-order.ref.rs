use std::collections::HashSet;
use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut seen = HashSet::new();
    let mut out: Vec<String> = Vec::new();
    for t in s.split_whitespace() {
        if seen.insert(t.to_string()) {
            out.push(t.to_string());
        }
    }
    println!("{}", out.join(" "));
}
