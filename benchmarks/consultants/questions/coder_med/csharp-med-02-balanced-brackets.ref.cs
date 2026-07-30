using System;
using System.Collections.Generic;
class Sol {
    static void Main() {
        string s = Console.ReadLine() ?? "";
        var st = new Stack<char>();
        bool ok = true;
        foreach (char c in s) {
            if (c == '(' || c == '[' || c == '{') st.Push(c);
            else if (c == ')' || c == ']' || c == '}') {
                char m = c == ')' ? '(' : (c == ']' ? '[' : '{');
                if (st.Count == 0 || st.Pop() != m) { ok = false; break; }
            }
        }
        Console.WriteLine(ok && st.Count == 0 ? "YES" : "NO");
    }
}
