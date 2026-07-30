using System;
class Sol {
    static void Main() {
        var tok = Console.In.ReadToEnd().Trim().Split()[0];
        long sum = 0;
        foreach (var c in tok)
            if (c >= '0' && c <= '9') sum += c - '0';
        Console.WriteLine(sum);
    }
}
