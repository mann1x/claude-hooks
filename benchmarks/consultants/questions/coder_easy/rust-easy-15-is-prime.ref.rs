use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: i64 = s.split_whitespace().next().unwrap().parse().unwrap();
    let mut p = n >= 2;
    let mut i = 2i64;
    while i * i <= n {
        if n % i == 0 {
            p = false;
            break;
        }
        i += 1;
    }
    println!("{}", if p { "yes" } else { "no" });
}
