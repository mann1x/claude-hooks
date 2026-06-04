using System;
using System.Collections.Generic;
class Sol {
    static void Main() {
        string s = (Console.ReadLine() ?? "").Trim();
        var nums = new List<long>();
        var ops = new List<char>();
        long cur = 0; bool have = false;
        foreach (char c in s) {
            if (c >= '0' && c <= '9') { cur = cur * 10 + (c - '0'); have = true; }
            else if (c == '+' || c == '-' || c == '*') {
                nums.Add(cur); cur = 0; have = false; ops.Add(c);
            }
        }
        if (have || nums.Count == 0) nums.Add(cur);
        var rn = new List<long> { nums[0] };
        var ro = new List<char>();
        for (int k = 0; k < ops.Count; k++) {
            if (ops[k] == '*') rn[rn.Count - 1] = rn[rn.Count - 1] * nums[k + 1];
            else { ro.Add(ops[k]); rn.Add(nums[k + 1]); }
        }
        long total = rn[0];
        for (int k = 0; k < ro.Count; k++) {
            if (ro[k] == '+') total += rn[k + 1]; else total -= rn[k + 1];
        }
        Console.WriteLine(total);
    }
}
