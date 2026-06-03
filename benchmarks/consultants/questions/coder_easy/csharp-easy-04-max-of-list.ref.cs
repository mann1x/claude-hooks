using System;
using System.Linq;
class Sol {
    static void Main() {
        var p = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        Console.WriteLine(p.Select(long.Parse).Max());
    }
}
