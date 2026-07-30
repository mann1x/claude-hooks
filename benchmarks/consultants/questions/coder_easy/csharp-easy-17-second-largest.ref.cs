using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Distinct().OrderByDescending(z => z).ToArray();
        Console.WriteLine(v[1]);
    }
}
