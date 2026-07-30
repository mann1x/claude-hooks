using System;
using System.Linq;
class Sol {
    static void Main() {
        var a = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        long best = a[0], cur = a[0];
        for (int i = 1; i < a.Length; i++) {
            cur = a[i] > cur + a[i] ? a[i] : cur + a[i];
            if (cur > best) best = cur;
        }
        Console.WriteLine(best);
    }
}
