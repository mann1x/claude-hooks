use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let m = s.split_whitespace().map(|x| x.parse::<i64>().unwrap()).min().unwrap();
    println!("{}", m);
}
