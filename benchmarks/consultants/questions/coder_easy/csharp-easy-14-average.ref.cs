using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        Console.WriteLine(v.Sum() / v.Length);
    }
}
