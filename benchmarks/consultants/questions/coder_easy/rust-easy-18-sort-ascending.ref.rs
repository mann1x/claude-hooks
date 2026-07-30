use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    v.sort();
    let parts: Vec<String> = v.iter().map(|x| x.to_string()).collect();
    println!("{}", parts.join(" "));
}
