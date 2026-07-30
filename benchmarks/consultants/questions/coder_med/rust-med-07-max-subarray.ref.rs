use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let a: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    let mut best = a[0];
    let mut cur = a[0];
    for &x in &a[1..] {
        cur = if x > cur + x { x } else { cur + x };
        if cur > best { best = cur; }
    }
    println!("{}", best);
}
