using System;
using System.Linq;
class Sol {
    static void Main() {
        var line = Console.ReadLine() ?? "";
        var rev = new string(line.Reverse().ToArray());
        Console.WriteLine(line == rev ? "yes" : "no");
    }
}
