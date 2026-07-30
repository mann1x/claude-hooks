using System;
class Sol {
    static void Main() {
        var p = Console.In.ReadToEnd().Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        long a = long.Parse(p[0]), b = long.Parse(p[1]);
        while (b != 0) { long t = b; b = a % b; a = t; }
        Console.WriteLine(a);
    }
}
