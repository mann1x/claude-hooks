using System;
using System.Text;
class Sol {
    static void Main() {
        string s = Console.ReadLine() ?? "";
        var sb = new StringBuilder();
        int i = 0, n = s.Length;
        while (i < n) {
            int j = i;
            while (j < n && s[j] == s[i]) j++;
            sb.Append(s[i]);
            sb.Append(j - i);
            i = j;
        }
        Console.WriteLine(sb.ToString());
    }
}
