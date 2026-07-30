use std::io::{self, Read};
use std::collections::HashMap;
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut c: HashMap<&str, i64> = HashMap::new();
    for w in s.split_whitespace() {
        *c.entry(w).or_insert(0) += 1;
    }
    let mut best = "";
    let mut bc = -1i64;
    for (w, n) in &c {
        if *n > bc || (*n == bc && *w < best) {
            bc = *n;
            best = w;
        }
    }
    println!("{}", best);
}
