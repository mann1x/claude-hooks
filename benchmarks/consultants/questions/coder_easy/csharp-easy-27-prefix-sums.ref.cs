using System;
using System.Collections.Generic;
using System.Linq;
class Sol {
    static void Main() {
        long acc = 0;
        var outp = new List<string>();
        foreach (var t in Console.In.ReadToEnd()
                 .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)) {
            acc += long.Parse(t);
            outp.Add(acc.ToString());
        }
        Console.WriteLine(string.Join(" ", outp));
    }
}
