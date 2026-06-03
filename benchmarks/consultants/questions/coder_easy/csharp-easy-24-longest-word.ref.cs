using System;
using System.Linq;
class Sol {
    static void Main() {
        var words = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        string best = "";
        foreach (var w in words)
            if (w.Length > best.Length) best = w;
        Console.WriteLine(best);
    }
}
