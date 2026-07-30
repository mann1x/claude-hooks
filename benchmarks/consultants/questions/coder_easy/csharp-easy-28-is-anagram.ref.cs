using System;
using System.Linq;
class Sol {
    static void Main() {
        var a = (Console.ReadLine() ?? "").OrderBy(c => c);
        var b = (Console.ReadLine() ?? "").OrderBy(c => c);
        Console.WriteLine(a.SequenceEqual(b) ? "yes" : "no");
    }
}
