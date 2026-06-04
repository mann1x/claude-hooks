use std::io::{self, Read};
use std::collections::VecDeque;
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let nums: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    let k = nums[0];
    let a = &nums[1..];
    let mut dq: VecDeque<usize> = VecDeque::new();
    let mut out: Vec<String> = Vec::new();
    for i in 0..a.len() {
        while let Some(&b) = dq.back() {
            if a[b] <= a[i] { dq.pop_back(); } else { break; }
        }
        dq.push_back(i);
        if (*dq.front().unwrap() as i64) <= i as i64 - k {
            dq.pop_front();
        }
        if i as i64 >= k - 1 {
            out.push(a[*dq.front().unwrap()].to_string());
        }
    }
    println!("{}", out.join(" "));
}
