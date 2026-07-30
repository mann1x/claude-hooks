use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut lines = s.lines();
    let a = lines.next().unwrap_or("");
    let b = lines.next().unwrap_or("");
    let mut ca: Vec<char> = a.chars().collect();
    let mut cb: Vec<char> = b.chars().collect();
    ca.sort();
    cb.sort();
    println!("{}", if ca == cb { "yes" } else { "no" });
}
