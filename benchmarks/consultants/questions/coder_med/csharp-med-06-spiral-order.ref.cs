using System;
using System.Linq;
using System.Collections.Generic;
class Sol {
    static void Main() {
        var nums = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(int.Parse).ToArray();
        int R = nums[0], C = nums[1];
        Func<int,int,int> at = (i, j) => nums[2 + i * C + j];
        int top = 0, bot = R - 1, left = 0, right = C - 1;
        var outp = new List<int>();
        while (top <= bot && left <= right) {
            for (int j = left; j <= right; j++) outp.Add(at(top, j));
            top++;
            for (int i = top; i <= bot; i++) outp.Add(at(i, right));
            right--;
            if (top <= bot) { for (int j = right; j >= left; j--) outp.Add(at(bot, j)); bot--; }
            if (left <= right) { for (int i = bot; i >= top; i--) outp.Add(at(i, left)); left++; }
        }
        Console.WriteLine(string.Join(" ", outp));
    }
}
