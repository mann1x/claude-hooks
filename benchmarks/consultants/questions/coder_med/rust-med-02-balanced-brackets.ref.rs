use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim_end_matches(|c| c == '\n' || c == '\r');
    let mut st: Vec<char> = Vec::new();
    let mut ok = true;
    for c in s.chars() {
        match c {
            '(' | '[' | '{' => st.push(c),
            ')' | ']' | '}' => {
                let m = match c { ')' => '(', ']' => '[', _ => '{' };
                if st.pop() != Some(m) { ok = false; break; }
            }
            _ => {}
        }
    }
    println!("{}", if ok && st.is_empty() { "YES" } else { "NO" });
}
