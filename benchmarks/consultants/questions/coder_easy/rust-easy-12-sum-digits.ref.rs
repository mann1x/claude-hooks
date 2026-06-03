use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let tok = s.split_whitespace().next().unwrap();
    let sum: u32 = tok.chars().map(|c| c.to_digit(10).unwrap()).sum();
    println!("{}", sum);
}
