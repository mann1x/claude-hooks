using System;
class Sol {
    static void Main() {
        var s = Console.In.ReadToEnd().ToLower();
        int n = 0;
        foreach (var ch in s)
            if ("aeiou".IndexOf(ch) >= 0) n++;
        Console.WriteLine(n);
    }
}
