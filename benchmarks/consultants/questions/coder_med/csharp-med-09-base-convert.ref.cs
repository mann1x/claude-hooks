using System;
class Sol {
    static long Dv(char c) {
        return (c >= '0' && c <= '9') ? c - '0' : c - 'a' + 10;
    }
    static void Main() {
        var parts = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        long fb = long.Parse(parts[0]), tb = long.Parse(parts[1]);
        long n = 0;
        foreach (char c in parts[2]) n = n * fb + Dv(c);
        if (n == 0) { Console.WriteLine("0"); return; }
        string digs = "0123456789abcdef";
        var chars = new System.Collections.Generic.List<char>();
        while (n > 0) { chars.Add(digs[(int)(n % tb)]); n /= tb; }
        chars.Reverse();
        Console.WriteLine(new string(chars.ToArray()));
    }
}
