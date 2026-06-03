use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let tok = s.split_whitespace().next().unwrap();
    println!("{}", i64::from_str_radix(tok, 2).unwrap());
}
