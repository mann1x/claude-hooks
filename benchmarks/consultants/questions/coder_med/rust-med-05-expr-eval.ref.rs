use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim();
    let mut nums: Vec<i64> = Vec::new();
    let mut ops: Vec<char> = Vec::new();
    let mut cur: i64 = 0;
    let mut have = false;
    for c in s.chars() {
        if c.is_ascii_digit() {
            cur = cur * 10 + (c as i64 - '0' as i64);
            have = true;
        } else if c == '+' || c == '-' || c == '*' {
            nums.push(cur);
            cur = 0;
            have = false;
            ops.push(c);
        }
    }
    if have || nums.is_empty() {
        nums.push(cur);
    }
    let mut rn: Vec<i64> = vec![nums[0]];
    let mut ro: Vec<char> = Vec::new();
    for k in 0..ops.len() {
        if ops[k] == '*' {
            let last = rn.len() - 1;
            rn[last] *= nums[k + 1];
        } else {
            ro.push(ops[k]);
            rn.push(nums[k + 1]);
        }
    }
    let mut total = rn[0];
    for k in 0..ro.len() {
        if ro[k] == '+' { total += rn[k + 1]; } else { total -= rn[k + 1]; }
    }
    println!("{}", total);
}
