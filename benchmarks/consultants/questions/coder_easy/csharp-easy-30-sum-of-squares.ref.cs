using System;
using System.Linq;
class Sol {
    static void Main() {
        var sum = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Sum(x => x * x);
        Console.WriteLine(sum);
    }
}
