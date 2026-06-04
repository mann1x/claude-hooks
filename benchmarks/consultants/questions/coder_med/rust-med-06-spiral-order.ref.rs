use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let nums: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    let r = nums[0] as i64;
    let c = nums[1] as i64;
    let g = &nums[2..];
    let at = |i: i64, j: i64| g[(i * c + j) as usize];
    let (mut top, mut bot, mut left, mut right) = (0i64, r - 1, 0i64, c - 1);
    let mut out: Vec<String> = Vec::new();
    while top <= bot && left <= right {
        for j in left..=right { out.push(at(top, j).to_string()); }
        top += 1;
        for i in top..=bot { out.push(at(i, right).to_string()); }
        right -= 1;
        if top <= bot {
            let mut j = right;
            while j >= left { out.push(at(bot, j).to_string()); j -= 1; }
            bot -= 1;
        }
        if left <= right {
            let mut i = bot;
            while i >= top { out.push(at(i, left).to_string()); i -= 1; }
            left += 1;
        }
    }
    println!("{}", out.join(" "));
}
