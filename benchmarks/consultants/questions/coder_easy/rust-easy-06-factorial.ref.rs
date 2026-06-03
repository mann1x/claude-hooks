use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: i64 = s.split_whitespace().next().unwrap().parse().unwrap();
    let mut r: i64 = 1;
    for i in 2..=n {
        r *= i;
    }
    println!("{}", r);
}
