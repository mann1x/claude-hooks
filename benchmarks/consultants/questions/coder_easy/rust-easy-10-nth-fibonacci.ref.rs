use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: u64 = s.split_whitespace().next().unwrap().parse().unwrap();
    let (mut a, mut b): (u64, u64) = (0, 1);
    for _ in 0..n {
        let t = a + b;
        a = b;
        b = t;
    }
    println!("{}", a);
}
