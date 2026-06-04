using System;
using System.Linq;
using System.Collections.Generic;
class Sol {
    static void Main() {
        var nums = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        long k = nums[0];
        var a = nums.Skip(1).ToArray();
        var dq = new LinkedList<int>();
        var outp = new List<long>();
        for (int i = 0; i < a.Length; i++) {
            while (dq.Count > 0 && a[dq.Last.Value] <= a[i]) dq.RemoveLast();
            dq.AddLast(i);
            if (dq.First.Value <= i - k) dq.RemoveFirst();
            if (i >= k - 1) outp.Add(a[dq.First.Value]);
        }
        Console.WriteLine(string.Join(" ", outp));
    }
}
