using System;
class Sol {
    static void Main() {
        int n = int.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        long r = 1;
        for (int i = 2; i <= n; i++) r *= i;
        Console.WriteLine(r);
    }
}
