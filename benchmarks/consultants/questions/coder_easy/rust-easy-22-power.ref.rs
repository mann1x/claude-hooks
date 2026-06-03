use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    let mut r: i64 = 1;
    for _ in 0..v[1] {
        r *= v[0];
    }
    println!("{}", r);
}
