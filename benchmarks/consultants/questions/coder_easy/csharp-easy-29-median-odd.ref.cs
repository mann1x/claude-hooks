using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).OrderBy(z => z).ToArray();
        Console.WriteLine(v[v.Length / 2]);
    }
}
