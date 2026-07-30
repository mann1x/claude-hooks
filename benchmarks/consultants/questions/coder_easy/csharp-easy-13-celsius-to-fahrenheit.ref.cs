using System;
class Sol {
    static void Main() {
        long c = long.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        Console.WriteLine(c * 9 / 5 + 32);
    }
}
