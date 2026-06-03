using System;
using System.Linq;
class Sol {
    static void Main() {
        var words = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(w => char.ToUpperInvariant(w[0]) + w.Substring(1).ToLowerInvariant());
        Console.WriteLine(string.Join(" ", words));
    }
}
