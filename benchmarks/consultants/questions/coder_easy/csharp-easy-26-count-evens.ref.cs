using System;
using System.Linq;
class Sol {
    static void Main() {
        var n = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Count(x => x % 2 == 0);
        Console.WriteLine(n);
    }
}
