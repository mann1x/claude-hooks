using System;
class Sol {
    static void Main() {
        var line = Console.ReadLine() ?? "";
        var a = line.ToCharArray();
        Array.Reverse(a);
        Console.WriteLine(new string(a));
    }
}
