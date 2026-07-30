use std::io::{self, Read};
fn dv(c: u8) -> i64 {
    if c.is_ascii_digit() { (c - b'0') as i64 } else { (c - b'a') as i64 + 10 }
}
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let parts: Vec<&str> = s.split_whitespace().collect();
    let fb: i64 = parts[0].parse().unwrap();
    let tb: i64 = parts[1].parse().unwrap();
    let mut n: i64 = 0;
    for &c in parts[2].as_bytes() {
        n = n * fb + dv(c);
    }
    if n == 0 {
        println!("0");
        return;
    }
    let digs = b"0123456789abcdef";
    let mut out: Vec<u8> = Vec::new();
    while n > 0 {
        out.push(digs[(n % tb) as usize]);
        n /= tb;
    }
    out.reverse();
    println!("{}", String::from_utf8(out).unwrap());
}
