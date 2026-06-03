using System;
class Sol {
    static void Main() {
        long n = long.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        Console.WriteLine(Convert.ToString(n, 2));
    }
}
