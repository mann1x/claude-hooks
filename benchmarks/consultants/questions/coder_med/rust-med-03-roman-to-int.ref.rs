use std::io::{self, Read};
fn v(c: u8) -> i64 {
    match c {
        b'I' => 1, b'V' => 5, b'X' => 10, b'L' => 50,
        b'C' => 100, b'D' => 500, b'M' => 1000, _ => 0,
    }
}
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim().as_bytes();
    let n = s.len();
    let mut total: i64 = 0;
    for i in 0..n {
        if i + 1 < n && v(s[i]) < v(s[i + 1]) {
            total -= v(s[i]);
        } else {
            total += v(s[i]);
        }
    }
    println!("{}", total);
}
