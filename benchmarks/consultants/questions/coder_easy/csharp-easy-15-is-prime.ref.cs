using System;
class Sol {
    static void Main() {
        long n = long.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        bool p = n >= 2;
        for (long i = 2; i * i <= n; i++)
            if (n % i == 0) { p = false; break; }
        Console.WriteLine(p ? "yes" : "no");
    }
}
