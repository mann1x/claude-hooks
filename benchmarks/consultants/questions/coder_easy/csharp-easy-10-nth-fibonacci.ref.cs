using System;
class Sol {
    static void Main() {
        int n = int.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        long a = 0, b = 1;
        for (int i = 0; i < n; i++) { long t = a + b; a = b; b = t; }
        Console.WriteLine(a);
    }
}
