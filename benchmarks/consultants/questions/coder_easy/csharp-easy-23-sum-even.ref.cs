using System;
using System.Linq;
class Sol {
    static void Main() {
        var sum = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Where(x => x % 2 == 0).Sum();
        Console.WriteLine(sum);
    }
}
