use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut best = "";
    for w in s.split_whitespace() {
        if w.len() > best.len() {
            best = w;
        }
    }
    println!("{}", best);
}
