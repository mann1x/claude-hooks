use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n = s.chars()
        .filter(|c| "aeiou".contains(c.to_ascii_lowercase()))
        .count();
    println!("{}", n);
}
