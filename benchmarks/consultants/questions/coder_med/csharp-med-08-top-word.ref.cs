using System;
using System.Linq;
using System.Collections.Generic;
class Sol {
    static void Main() {
        var words = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        var c = new Dictionary<string,long>();
        foreach (var w in words) {
            c.TryGetValue(w, out long v);
            c[w] = v + 1;
        }
        string best = null; long bc = -1;
        foreach (var kv in c) {
            if (kv.Value > bc || (kv.Value == bc && string.CompareOrdinal(kv.Key, best) < 0)) {
                bc = kv.Value; best = kv.Key;
            }
        }
        Console.WriteLine(best);
    }
}
