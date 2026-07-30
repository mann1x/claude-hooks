using System;
using System.Collections.Generic;
class Sol {
    static int V(char c) {
        switch (c) {
            case 'I': return 1; case 'V': return 5; case 'X': return 10;
            case 'L': return 50; case 'C': return 100; case 'D': return 500;
            case 'M': return 1000; default: return 0;
        }
    }
    static void Main() {
        string s = (Console.ReadLine() ?? "").Trim();
        long total = 0;
        int n = s.Length;
        for (int i = 0; i < n; i++) {
            if (i + 1 < n && V(s[i]) < V(s[i + 1])) total -= V(s[i]);
            else total += V(s[i]);
        }
        Console.WriteLine(total);
    }
}
