using System;
using System.Linq;
class Sol {
    static void Main() {
        var parts = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'},
                   StringSplitOptions.RemoveEmptyEntries);
        long s = parts.Sum(p => long.Parse(p));
        Console.WriteLine(s);
    }
}
