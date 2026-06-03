use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    let (mut a, mut b) = (v[0], v[1]);
    while b != 0 {
        let t = b;
        b = a % b;
        a = t;
    }
    println!("{}", a);
}
