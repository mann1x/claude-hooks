use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let nums: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    if nums.is_empty() { return; }
    let n = nums[0] as usize;
    let mut iv: Vec<(i64, i64)> = (0..n)
        .map(|i| (nums[1 + 2 * i], nums[2 + 2 * i])).collect();
    iv.sort();
    let mut out: Vec<(i64, i64)> = Vec::new();
    for (st, en) in iv {
        if let Some(last) = out.last_mut() {
            if st <= last.1 {
                if en > last.1 { last.1 = en; }
                continue;
            }
        }
        out.push((st, en));
    }
    let parts: Vec<String> = out.iter()
        .flat_map(|&(s, e)| vec![s.to_string(), e.to_string()]).collect();
    println!("{}", parts.join(" "));
}
