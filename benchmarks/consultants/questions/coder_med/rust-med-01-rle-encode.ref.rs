use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim_end();
    let b = s.as_bytes();
    let n = b.len();
    let mut i = 0;
    let mut out = String::new();
    while i < n {
        let mut j = i;
        while j < n && b[j] == b[i] {
            j += 1;
        }
        out.push(b[i] as char);
        out.push_str(&(j - i).to_string());
        i = j;
    }
    println!("{}", out);
}
