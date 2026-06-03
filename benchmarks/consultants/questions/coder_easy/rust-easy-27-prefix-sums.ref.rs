use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut acc: i64 = 0;
    let parts: Vec<String> = s.split_whitespace().map(|x| {
        acc += x.parse::<i64>().unwrap();
        acc.to_string()
    }).collect();
    println!("{}", parts.join(" "));
}
