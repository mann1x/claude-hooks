using System;
using System.Linq;
using System.Collections.Generic;
class Sol {
    static void Main() {
        var nums = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        if (nums.Length == 0) return;
        int n = (int)nums[0];
        var iv = new List<(long s, long e)>();
        for (int i = 0; i < n; i++) iv.Add((nums[1 + 2 * i], nums[2 + 2 * i]));
        iv.Sort((a, b) => a.s != b.s ? a.s.CompareTo(b.s) : a.e.CompareTo(b.e));
        var outp = new List<(long s, long e)>();
        foreach (var p in iv) {
            if (outp.Count > 0 && p.s <= outp[^1].e) {
                if (p.e > outp[^1].e) outp[^1] = (outp[^1].s, p.e);
            } else outp.Add(p);
        }
        Console.WriteLine(string.Join(" ",
            outp.SelectMany(p => new[]{p.s, p.e})));
    }
}
