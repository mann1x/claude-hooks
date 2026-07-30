using System;
class Sol {
    static void Main() {
        var tok = Console.In.ReadToEnd().Trim().Split()[0];
        Console.WriteLine(Convert.ToInt64(tok, 2));
    }
}
