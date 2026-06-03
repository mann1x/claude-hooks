#!/usr/bin/env python3
"""Generator for the ``coder_easy`` skill-eval suite.

The easy suite exists because ``coder_mlang@1.0.1`` was *too hard*: 10 of
its 13 questions defeated every model, so the per-language picks had to be
normalized over the 3 questions that discriminated (see
``docs/benchmarks/coder-mlang-results.md``). This suite measures the
opposite end — can a model reliably solve *easy* problems in each
language? — with enough questions (30 per language) that pass-rate is a
meaningful signal.

Every question uses ONE uniform contract across all six languages:

    read from standard input  ->  write the answer to standard output

so a problem's oracle is identical regardless of language (only ``lang``
and the source filename change). The model writes ``solution.<ext>``; the
oracle runs it via ``oracles_mlang.run_program`` (Python = direct
subprocess; the five compiled languages = ``compile_and_run``).

This generator is the source of truth. It emits, per (problem × language):

    <lang>-easy-NN-<slug>.md          model-facing task
    <lang>-easy-NN-<slug>-oracle.py   harness-facing pytest oracle
    <lang>-easy-NN-<slug>.ref<ext>    reference solution (dry-run only)

plus ``SUITE.md`` (manifest + rubric) and ``conftest.py`` (markers).

Run:  python benchmarks/consultants/gen_coder_easy.py
Then: python benchmarks/consultants/coder_bench.py --dry-run \
          --questions-dir benchmarks/consultants/questions/coder_easy
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# --------------------------------------------------------------------- #
# Language config
# --------------------------------------------------------------------- #
LANGS: dict[str, str] = {
    "python": ".py",
    "rust":   ".rs",
    "go":     ".go",
    "c":      ".c",
    "cpp":    ".cpp",
    "csharp": ".cs",
}
DISPLAY = {
    "python": "Python", "rust": "Rust", "go": "Go",
    "c": "C", "cpp": "C++", "csharp": "C#",
}
SUITE_DIR = (
    Path(__file__).resolve().parent / "questions" / "coder_easy"
)
SUITE_VERSION = "1.0"


# --------------------------------------------------------------------- #
# Problem spec
# --------------------------------------------------------------------- #
# Each problem:
#   slug      short kebab id fragment
#   title     human title
#   desc      language-agnostic statement
#   stdin     input-format description (model-facing)
#   stdout    output-format description (model-facing)
#   cases     list of (stdin_str, expected_stdout_str), shared across langs
#   refs      {lang: source_code}  — a stdin->stdout program per language
#
# The oracle compares ``stdout.strip() == expected.strip()`` so trailing
# newlines never matter; avoid cases whose answer has meaningful leading/
# trailing whitespace.
# --------------------------------------------------------------------- #
PROBLEMS: list[dict] = [
    {
        "slug": "sum-list",
        "title": "Sum of integers",
        "desc": "Read a list of integers and print their sum.",
        "stdin": "one line of whitespace-separated integers (may be negative)",
        "stdout": "a single integer: the sum",
        "cases": [
            ("1 2 3 4\n", "10"),
            ("5\n", "5"),
            ("-3 3 10\n", "10"),
            ("100 200 300\n", "600"),
            ("-1 -2 -3\n", "-6"),
            ("0 0 0\n", "0"),
        ],
        "refs": {
            "python": (
                "import sys\n"
                "print(sum(int(x) for x in sys.stdin.read().split()))\n"
            ),
            "rust": (
                "use std::io::{self, Read};\n"
                "fn main() {\n"
                "    let mut s = String::new();\n"
                "    io::stdin().read_to_string(&mut s).unwrap();\n"
                "    let sum: i64 = s.split_whitespace()\n"
                "        .map(|x| x.parse::<i64>().unwrap()).sum();\n"
                "    println!(\"{}\", sum);\n"
                "}\n"
            ),
            "go": (
                "package main\n\n"
                "import (\n\t\"fmt\"\n\t\"io\"\n\t\"os\"\n"
                "\t\"strconv\"\n\t\"strings\"\n)\n\n"
                "func main() {\n"
                "\tdata, _ := io.ReadAll(os.Stdin)\n"
                "\tsum := 0\n"
                "\tfor _, f := range strings.Fields(string(data)) {\n"
                "\t\tn, _ := strconv.Atoi(f)\n"
                "\t\tsum += n\n"
                "\t}\n"
                "\tfmt.Println(sum)\n"
                "}\n"
            ),
            "c": (
                "#include <stdio.h>\n"
                "int main(void) {\n"
                "    long long n, s = 0;\n"
                "    while (scanf(\"%lld\", &n) == 1) s += n;\n"
                "    printf(\"%lld\\n\", s);\n"
                "    return 0;\n"
                "}\n"
            ),
            "cpp": (
                "#include <iostream>\n"
                "int main() {\n"
                "    long long n, s = 0;\n"
                "    while (std::cin >> n) s += n;\n"
                "    std::cout << s << std::endl;\n"
                "    return 0;\n"
                "}\n"
            ),
            "csharp": (
                "using System;\n"
                "using System.Linq;\n"
                "class Sol {\n"
                "    static void Main() {\n"
                "        var parts = Console.In.ReadToEnd()\n"
                "            .Split(new[]{' ','\\n','\\r','\\t'},\n"
                "                   StringSplitOptions.RemoveEmptyEntries);\n"
                "        long s = parts.Sum(p => long.Parse(p));\n"
                "        Console.WriteLine(s);\n"
                "    }\n"
                "}\n"
            ),
        },
    },
    {
        "slug": "reverse-string",
        "title": "Reverse a string",
        "desc": "Read one line and print it with its characters reversed.",
        "stdin": "a single line of text",
        "stdout": "the line reversed character-by-character",
        "cases": [
            ("hello\n", "olleh"),
            ("ab cd\n", "dc ba"),
            ("racecar\n", "racecar"),
            ("12345\n", "54321"),
            ("a\n", "a"),
            ("Hello, World!\n", "!dlroW ,olleH"),
        ],
        "refs": {
            "python": (
                "import sys\n"
                "line = sys.stdin.readline().rstrip('\\n').rstrip('\\r')\n"
                "print(line[::-1])\n"
            ),
            "rust": (
                "use std::io::{self, BufRead};\n"
                "fn main() {\n"
                "    let mut s = String::new();\n"
                "    io::stdin().lock().read_line(&mut s).unwrap();\n"
                "    let s = s.trim_end_matches(['\\n', '\\r']);\n"
                "    let r: String = s.chars().rev().collect();\n"
                "    println!(\"{}\", r);\n"
                "}\n"
            ),
            "go": (
                "package main\n\n"
                "import (\n\t\"bufio\"\n\t\"fmt\"\n\t\"os\"\n\t\"strings\"\n)\n\n"
                "func main() {\n"
                "\tr := bufio.NewReader(os.Stdin)\n"
                "\tline, _ := r.ReadString('\\n')\n"
                "\tline = strings.TrimRight(line, \"\\r\\n\")\n"
                "\trunes := []rune(line)\n"
                "\tfor i, j := 0, len(runes)-1; i < j; i, j = i+1, j-1 {\n"
                "\t\trunes[i], runes[j] = runes[j], runes[i]\n"
                "\t}\n"
                "\tfmt.Println(string(runes))\n"
                "}\n"
            ),
            "c": (
                "#include <stdio.h>\n"
                "#include <string.h>\n"
                "int main(void) {\n"
                "    char buf[100000];\n"
                "    if (!fgets(buf, sizeof buf, stdin)) buf[0] = 0;\n"
                "    size_t n = strlen(buf);\n"
                "    while (n > 0 && (buf[n-1] == '\\n' || buf[n-1] == '\\r'))\n"
                "        buf[--n] = 0;\n"
                "    for (size_t i = 0; i < n / 2; i++) {\n"
                "        char t = buf[i]; buf[i] = buf[n-1-i]; buf[n-1-i] = t;\n"
                "    }\n"
                "    printf(\"%s\\n\", buf);\n"
                "    return 0;\n"
                "}\n"
            ),
            "cpp": (
                "#include <iostream>\n"
                "#include <string>\n"
                "#include <algorithm>\n"
                "int main() {\n"
                "    std::string s;\n"
                "    std::getline(std::cin, s);\n"
                "    while (!s.empty() && (s.back() == '\\r' || s.back() == '\\n'))\n"
                "        s.pop_back();\n"
                "    std::reverse(s.begin(), s.end());\n"
                "    std::cout << s << std::endl;\n"
                "    return 0;\n"
                "}\n"
            ),
            "csharp": (
                "using System;\n"
                "class Sol {\n"
                "    static void Main() {\n"
                "        var line = Console.ReadLine() ?? \"\";\n"
                "        var a = line.ToCharArray();\n"
                "        Array.Reverse(a);\n"
                "        Console.WriteLine(new string(a));\n"
                "    }\n"
                "}\n"
            ),
        },
    },
    {
        "slug": "count-vowels",
        "title": "Count vowels",
        "desc": (
            "Count the vowels (a, e, i, o, u; case-insensitive) in the "
            "input and print the count."
        ),
        "stdin": "one line of text",
        "stdout": "a single integer: the number of vowels",
        "cases": [
            ("hello\n", "2"),
            ("HELLO WORLD\n", "3"),
            ("xyz\n", "0"),
            ("AeIoU\n", "5"),
            ("programming\n", "3"),
            ("\n", "0"),
        ],
        "refs": {
            "python": (
                "import sys\n"
                "text = sys.stdin.read().lower()\n"
                "print(sum(1 for c in text if c in 'aeiou'))\n"
            ),
            "rust": (
                "use std::io::{self, Read};\n"
                "fn main() {\n"
                "    let mut s = String::new();\n"
                "    io::stdin().read_to_string(&mut s).unwrap();\n"
                "    let n = s.chars()\n"
                "        .filter(|c| \"aeiou\".contains(c.to_ascii_lowercase()))\n"
                "        .count();\n"
                "    println!(\"{}\", n);\n"
                "}\n"
            ),
            "go": (
                "package main\n\n"
                "import (\n\t\"fmt\"\n\t\"io\"\n\t\"os\"\n\t\"strings\"\n)\n\n"
                "func main() {\n"
                "\tdata, _ := io.ReadAll(os.Stdin)\n"
                "\ts := strings.ToLower(string(data))\n"
                "\tn := 0\n"
                "\tfor _, ch := range s {\n"
                "\t\tif strings.ContainsRune(\"aeiou\", ch) {\n"
                "\t\t\tn++\n"
                "\t\t}\n"
                "\t}\n"
                "\tfmt.Println(n)\n"
                "}\n"
            ),
            "c": (
                "#include <stdio.h>\n"
                "#include <ctype.h>\n"
                "int main(void) {\n"
                "    int ch, n = 0;\n"
                "    while ((ch = getchar()) != EOF) {\n"
                "        int l = tolower(ch);\n"
                "        if (l=='a'||l=='e'||l=='i'||l=='o'||l=='u') n++;\n"
                "    }\n"
                "    printf(\"%d\\n\", n);\n"
                "    return 0;\n"
                "}\n"
            ),
            "cpp": (
                "#include <iostream>\n"
                "#include <cctype>\n"
                "int main() {\n"
                "    char ch; int n = 0;\n"
                "    while (std::cin.get(ch)) {\n"
                "        char l = (char)std::tolower((unsigned char)ch);\n"
                "        if (l=='a'||l=='e'||l=='i'||l=='o'||l=='u') n++;\n"
                "    }\n"
                "    std::cout << n << std::endl;\n"
                "    return 0;\n"
                "}\n"
            ),
            "csharp": (
                "using System;\n"
                "class Sol {\n"
                "    static void Main() {\n"
                "        var s = Console.In.ReadToEnd().ToLower();\n"
                "        int n = 0;\n"
                "        foreach (var ch in s)\n"
                "            if (\"aeiou\".IndexOf(ch) >= 0) n++;\n"
                "        Console.WriteLine(n);\n"
                "    }\n"
                "}\n"
            ),
        },
    },
    {
        "slug": "max-of-list",
        "title": "Maximum of a list",
        "desc": "Read a list of integers and print the largest.",
        "stdin": "whitespace-separated integers (at least one)",
        "stdout": "a single integer: the maximum",
        "cases": [("3 1 4 1 5\n", "5"), ("-1 -2 -3\n", "-1"),
                  ("7\n", "7"), ("10 10 10\n", "10"), ("-5 0 5\n", "5")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(max(nums))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let m = s.split_whitespace().map(|x| x.parse::<i64>().unwrap()).max().unwrap();
    println!("{}", m);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x, m int
    if _, err := fmt.Scan(&m); err != nil {
        return
    }
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if x > m {
            m = x
        }
    }
    fmt.Println(m)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long x, m;
    if (scanf("%lld", &m) != 1) return 0;
    while (scanf("%lld", &x) == 1)
        if (x > m) m = x;
    printf("%lld\n", m);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long x, m;
    if (!(std::cin >> m)) return 0;
    while (std::cin >> x)
        if (x > m) m = x;
    std::cout << m << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var p = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        Console.WriteLine(p.Select(long.Parse).Max());
    }
}
""",
        },
    },
    {
        "slug": "min-of-list",
        "title": "Minimum of a list",
        "desc": "Read a list of integers and print the smallest.",
        "stdin": "whitespace-separated integers (at least one)",
        "stdout": "a single integer: the minimum",
        "cases": [("3 1 4 1 5\n", "1"), ("-1 -2 -3\n", "-3"),
                  ("7\n", "7"), ("10 10 10\n", "10"), ("-5 0 5\n", "-5")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(min(nums))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let m = s.split_whitespace().map(|x| x.parse::<i64>().unwrap()).min().unwrap();
    println!("{}", m);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x, m int
    if _, err := fmt.Scan(&m); err != nil {
        return
    }
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if x < m {
            m = x
        }
    }
    fmt.Println(m)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long x, m;
    if (scanf("%lld", &m) != 1) return 0;
    while (scanf("%lld", &x) == 1)
        if (x < m) m = x;
    printf("%lld\n", m);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long x, m;
    if (!(std::cin >> m)) return 0;
    while (std::cin >> x)
        if (x < m) m = x;
    std::cout << m << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var p = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        Console.WriteLine(p.Select(long.Parse).Min());
    }
}
""",
        },
    },
    {
        "slug": "factorial",
        "title": "Factorial",
        "desc": "Read a non-negative integer n (n ≤ 12) and print n! (n factorial).",
        "stdin": "a single integer n, 0 ≤ n ≤ 12",
        "stdout": "a single integer: n!",
        "cases": [("0\n", "1"), ("1\n", "1"), ("5\n", "120"),
                  ("10\n", "3628800"), ("12\n", "479001600")],
        "refs": {
            "python": r"""import sys
n = int(sys.stdin.read().split()[0])
r = 1
for i in range(2, n + 1):
    r *= i
print(r)
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: i64 = s.split_whitespace().next().unwrap().parse().unwrap();
    let mut r: i64 = 1;
    for i in 2..=n {
        r *= i;
    }
    println!("{}", r);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var n int
    fmt.Scan(&n)
    r := int64(1)
    for i := int64(2); i <= int64(n); i++ {
        r *= i
    }
    fmt.Println(r)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    int n;
    if (scanf("%d", &n) != 1) return 0;
    long long r = 1;
    for (int i = 2; i <= n; i++) r *= i;
    printf("%lld\n", r);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    int n;
    std::cin >> n;
    long long r = 1;
    for (int i = 2; i <= n; i++) r *= i;
    std::cout << r << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        int n = int.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        long r = 1;
        for (int i = 2; i <= n; i++) r *= i;
        Console.WriteLine(r);
    }
}
""",
        },
    },
    {
        "slug": "is-palindrome",
        "title": "Palindrome check",
        "desc": "Read one line and print \"yes\" if it reads the same forwards and backwards, otherwise \"no\". Comparison is exact (case-sensitive).",
        "stdin": "a single line of text",
        "stdout": "\"yes\" or \"no\"",
        "cases": [("racecar\n", "yes"), ("hello\n", "no"),
                  ("abba\n", "yes"), ("a\n", "yes"), ("ab\n", "no")],
        "refs": {
            "python": r"""import sys
line = sys.stdin.readline().rstrip('\n').rstrip('\r')
print("yes" if line == line[::-1] else "no")
""",
            "rust": r"""use std::io::{self, BufRead};
fn main() {
    let mut s = String::new();
    io::stdin().lock().read_line(&mut s).unwrap();
    let s = s.trim_end_matches(['\n', '\r']);
    let rev: String = s.chars().rev().collect();
    println!("{}", if s == rev { "yes" } else { "no" });
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "strings"
)

func main() {
    r := bufio.NewReader(os.Stdin)
    line, _ := r.ReadString('\n')
    line = strings.TrimRight(line, "\r\n")
    runes := []rune(line)
    ok := true
    for i, j := 0, len(runes)-1; i < j; i, j = i+1, j-1 {
        if runes[i] != runes[j] {
            ok = false
            break
        }
    }
    if ok {
        fmt.Println("yes")
    } else {
        fmt.Println("no")
    }
}
""",
            "c": r"""#include <stdio.h>
#include <string.h>
int main(void) {
    char buf[100000];
    if (!fgets(buf, sizeof buf, stdin)) buf[0] = 0;
    size_t n = strlen(buf);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = 0;
    int ok = 1;
    for (size_t i = 0; i < n / 2; i++)
        if (buf[i] != buf[n-1-i]) { ok = 0; break; }
    printf("%s\n", ok ? "yes" : "no");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
int main() {
    std::string s;
    std::getline(std::cin, s);
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    std::string r(s.rbegin(), s.rend());
    std::cout << (s == r ? "yes" : "no") << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var line = Console.ReadLine() ?? "";
        var rev = new string(line.Reverse().ToArray());
        Console.WriteLine(line == rev ? "yes" : "no");
    }
}
""",
        },
    },
    {
        "slug": "fizzbuzz",
        "title": "FizzBuzz",
        "desc": "Read an integer n and print the lines 1..n. For multiples of 3 print \"Fizz\", for multiples of 5 print \"Buzz\", for multiples of both print \"FizzBuzz\", otherwise print the number. One value per line.",
        "stdin": "a single integer n (n ≥ 1)",
        "stdout": "n lines, the FizzBuzz value for 1..n",
        "cases": [("5\n", "1\n2\nFizz\n4\nBuzz"), ("3\n", "1\n2\nFizz"),
                  ("1\n", "1"), ("2\n", "1\n2"),
                  ("15\n", "1\n2\nFizz\n4\nBuzz\nFizz\n7\n8\nFizz\nBuzz\n11\nFizz\n13\n14\nFizzBuzz")],
        "refs": {
            "python": r"""import sys
n = int(sys.stdin.read().split()[0])
out = []
for i in range(1, n + 1):
    if i % 15 == 0:
        out.append("FizzBuzz")
    elif i % 3 == 0:
        out.append("Fizz")
    elif i % 5 == 0:
        out.append("Buzz")
    else:
        out.append(str(i))
print("\n".join(out))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: i64 = s.split_whitespace().next().unwrap().parse().unwrap();
    for i in 1..=n {
        if i % 15 == 0 {
            println!("FizzBuzz");
        } else if i % 3 == 0 {
            println!("Fizz");
        } else if i % 5 == 0 {
            println!("Buzz");
        } else {
            println!("{}", i);
        }
    }
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var n int
    fmt.Scan(&n)
    for i := 1; i <= n; i++ {
        switch {
        case i%15 == 0:
            fmt.Println("FizzBuzz")
        case i%3 == 0:
            fmt.Println("Fizz")
        case i%5 == 0:
            fmt.Println("Buzz")
        default:
            fmt.Println(i)
        }
    }
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    int n;
    if (scanf("%d", &n) != 1) return 0;
    for (int i = 1; i <= n; i++) {
        if (i % 15 == 0) printf("FizzBuzz\n");
        else if (i % 3 == 0) printf("Fizz\n");
        else if (i % 5 == 0) printf("Buzz\n");
        else printf("%d\n", i);
    }
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    int n;
    std::cin >> n;
    for (int i = 1; i <= n; i++) {
        if (i % 15 == 0) std::cout << "FizzBuzz\n";
        else if (i % 3 == 0) std::cout << "Fizz\n";
        else if (i % 5 == 0) std::cout << "Buzz\n";
        else std::cout << i << "\n";
    }
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        int n = int.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        for (int i = 1; i <= n; i++) {
            if (i % 15 == 0) Console.WriteLine("FizzBuzz");
            else if (i % 3 == 0) Console.WriteLine("Fizz");
            else if (i % 5 == 0) Console.WriteLine("Buzz");
            else Console.WriteLine(i);
        }
    }
}
""",
        },
    },
    {
        "slug": "gcd",
        "title": "Greatest common divisor",
        "desc": "Read two positive integers and print their greatest common divisor.",
        "stdin": "two whitespace-separated positive integers a and b",
        "stdout": "a single integer: gcd(a, b)",
        "cases": [("12 18\n", "6"), ("17 5\n", "1"), ("100 10\n", "10"),
                  ("7 7\n", "7"), ("48 36\n", "12")],
        "refs": {
            "python": r"""import sys
a, b = (int(x) for x in sys.stdin.read().split()[:2])
while b:
    a, b = b, a % b
print(a)
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    let (mut a, mut b) = (v[0], v[1]);
    while b != 0 {
        let t = b;
        b = a % b;
        a = t;
    }
    println!("{}", a);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var a, b int
    fmt.Scan(&a, &b)
    for b != 0 {
        a, b = b, a%b
    }
    fmt.Println(a)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long a, b;
    if (scanf("%lld %lld", &a, &b) != 2) return 0;
    while (b) { long long t = b; b = a % b; a = t; }
    printf("%lld\n", a);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long a, b;
    std::cin >> a >> b;
    while (b) { long long t = b; b = a % b; a = t; }
    std::cout << a << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        var p = Console.In.ReadToEnd().Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        long a = long.Parse(p[0]), b = long.Parse(p[1]);
        while (b != 0) { long t = b; b = a % b; a = t; }
        Console.WriteLine(a);
    }
}
""",
        },
    },
    {
        "slug": "nth-fibonacci",
        "title": "Nth Fibonacci",
        "desc": "Read an integer n (0-indexed) and print the nth Fibonacci number, where F(0)=0, F(1)=1.",
        "stdin": "a single integer n, 0 ≤ n ≤ 90",
        "stdout": "a single integer: F(n)",
        "cases": [("0\n", "0"), ("1\n", "1"), ("10\n", "55"),
                  ("20\n", "6765"), ("50\n", "12586269025")],
        "refs": {
            "python": r"""import sys
n = int(sys.stdin.read().split()[0])
a, b = 0, 1
for _ in range(n):
    a, b = b, a + b
print(a)
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: u64 = s.split_whitespace().next().unwrap().parse().unwrap();
    let (mut a, mut b): (u64, u64) = (0, 1);
    for _ in 0..n {
        let t = a + b;
        a = b;
        b = t;
    }
    println!("{}", a);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var n int
    fmt.Scan(&n)
    var a, b int64 = 0, 1
    for i := 0; i < n; i++ {
        a, b = b, a+b
    }
    fmt.Println(a)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    int n;
    if (scanf("%d", &n) != 1) return 0;
    long long a = 0, b = 1;
    for (int i = 0; i < n; i++) { long long t = a + b; a = b; b = t; }
    printf("%lld\n", a);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    int n;
    std::cin >> n;
    long long a = 0, b = 1;
    for (int i = 0; i < n; i++) { long long t = a + b; a = b; b = t; }
    std::cout << a << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        int n = int.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        long a = 0, b = 1;
        for (int i = 0; i < n; i++) { long t = a + b; a = b; b = t; }
        Console.WriteLine(a);
    }
}
""",
        },
    },
    {
        "slug": "count-words",
        "title": "Count words",
        "desc": "Read text and print the number of whitespace-separated words.",
        "stdin": "a line of text (words separated by spaces)",
        "stdout": "a single integer: the word count",
        "cases": [("the quick brown fox\n", "4"), ("hello\n", "1"),
                  ("  a  b  \n", "2"), ("one two three\n", "3"), ("\n", "0")],
        "refs": {
            "python": r"""import sys
print(len(sys.stdin.read().split()))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    println!("{}", s.split_whitespace().count());
}
""",
            "go": r"""package main

import (
    "fmt"
    "io"
    "os"
    "strings"
)

func main() {
    data, _ := io.ReadAll(os.Stdin)
    fmt.Println(len(strings.Fields(string(data))))
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    char buf[1 << 16];
    int count = 0;
    while (scanf("%65535s", buf) == 1) count++;
    printf("%d\n", count);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
int main() {
    std::string w;
    int count = 0;
    while (std::cin >> w) count++;
    std::cout << count << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        var p = Console.In.ReadToEnd().Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        Console.WriteLine(p.Length);
    }
}
""",
        },
    },
    {
        "slug": "sum-digits",
        "title": "Sum of digits",
        "desc": "Read a non-negative integer (as text) and print the sum of its decimal digits.",
        "stdin": "a single non-negative integer, possibly very large",
        "stdout": "a single integer: the digit sum",
        "cases": [("123\n", "6"), ("0\n", "0"), ("9999\n", "36"),
                  ("1000000\n", "1"), ("505\n", "10")],
        "refs": {
            "python": r"""import sys
tok = sys.stdin.read().split()[0]
print(sum(int(c) for c in tok))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let tok = s.split_whitespace().next().unwrap();
    let sum: u32 = tok.chars().map(|c| c.to_digit(10).unwrap()).sum();
    println!("{}", sum);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var tok string
    fmt.Scan(&tok)
    sum := 0
    for _, c := range tok {
        sum += int(c - '0')
    }
    fmt.Println(sum)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    char buf[1 << 16];
    if (scanf("%65535s", buf) != 1) { printf("0\n"); return 0; }
    long sum = 0;
    for (int i = 0; buf[i]; i++)
        if (buf[i] >= '0' && buf[i] <= '9') sum += buf[i] - '0';
    printf("%ld\n", sum);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
int main() {
    std::string tok;
    std::cin >> tok;
    long sum = 0;
    for (char c : tok)
        if (c >= '0' && c <= '9') sum += c - '0';
    std::cout << sum << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        var tok = Console.In.ReadToEnd().Trim().Split()[0];
        long sum = 0;
        foreach (var c in tok)
            if (c >= '0' && c <= '9') sum += c - '0';
        Console.WriteLine(sum);
    }
}
""",
        },
    },
    {
        "slug": "celsius-to-fahrenheit",
        "title": "Celsius to Fahrenheit",
        "desc": "Read an integer temperature in Celsius (a multiple of 5) and print it in Fahrenheit, computed as C*9/5+32.",
        "stdin": "a single integer Celsius value (a multiple of 5)",
        "stdout": "a single integer: the Fahrenheit value",
        "cases": [("0\n", "32"), ("100\n", "212"), ("-40\n", "-40"),
                  ("5\n", "41"), ("-5\n", "23")],
        "refs": {
            "python": r"""import sys
c = int(sys.stdin.read().split()[0])
print(c * 9 // 5 + 32)
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let c: i64 = s.split_whitespace().next().unwrap().parse().unwrap();
    println!("{}", c * 9 / 5 + 32);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var c int
    fmt.Scan(&c)
    fmt.Println(c*9/5 + 32)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long c;
    if (scanf("%ld", &c) != 1) return 0;
    printf("%ld\n", c * 9 / 5 + 32);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long c;
    std::cin >> c;
    std::cout << (c * 9 / 5 + 32) << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        long c = long.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        Console.WriteLine(c * 9 / 5 + 32);
    }
}
""",
        },
    },
    {
        "slug": "average",
        "title": "Integer average",
        "desc": "Read a list of integers whose sum divides evenly by their count, and print their integer average (sum divided by count).",
        "stdin": "whitespace-separated integers",
        "stdout": "a single integer: the average",
        "cases": [("2 4 6\n", "4"), ("10 20\n", "15"), ("5 5 5\n", "5"),
                  ("1 2 3 4 5\n", "3"), ("100\n", "100")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sum(nums) // len(nums))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    let sum: i64 = v.iter().sum();
    println!("{}", sum / v.len() as i64);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x int
    sum, count := 0, 0
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        sum += x
        count++
    }
    fmt.Println(sum / count)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long x, sum = 0;
    long count = 0;
    while (scanf("%lld", &x) == 1) { sum += x; count++; }
    printf("%lld\n", sum / count);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long x, sum = 0;
    long count = 0;
    while (std::cin >> x) { sum += x; count++; }
    std::cout << (sum / count) << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        Console.WriteLine(v.Sum() / v.Length);
    }
}
""",
        },
    },
    {
        "slug": "is-prime",
        "title": "Primality test",
        "desc": "Read an integer n and print \"yes\" if it is prime, otherwise \"no\". (Numbers below 2 are not prime.)",
        "stdin": "a single integer n",
        "stdout": "\"yes\" or \"no\"",
        "cases": [("2\n", "yes"), ("1\n", "no"), ("17\n", "yes"),
                  ("18\n", "no"), ("97\n", "yes")],
        "refs": {
            "python": r"""import sys
n = int(sys.stdin.read().split()[0])
if n < 2:
    print("no")
else:
    p = True
    i = 2
    while i * i <= n:
        if n % i == 0:
            p = False
            break
        i += 1
    print("yes" if p else "no")
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: i64 = s.split_whitespace().next().unwrap().parse().unwrap();
    let mut p = n >= 2;
    let mut i = 2i64;
    while i * i <= n {
        if n % i == 0 {
            p = false;
            break;
        }
        i += 1;
    }
    println!("{}", if p { "yes" } else { "no" });
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var n int
    fmt.Scan(&n)
    p := n >= 2
    for i := 2; i*i <= n; i++ {
        if n%i == 0 {
            p = false
            break
        }
    }
    if p {
        fmt.Println("yes")
    } else {
        fmt.Println("no")
    }
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long n;
    if (scanf("%lld", &n) != 1) return 0;
    int p = n >= 2;
    for (long long i = 2; i * i <= n; i++)
        if (n % i == 0) { p = 0; break; }
    printf("%s\n", p ? "yes" : "no");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long n;
    std::cin >> n;
    bool p = n >= 2;
    for (long long i = 2; i * i <= n; i++)
        if (n % i == 0) { p = false; break; }
    std::cout << (p ? "yes" : "no") << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        long n = long.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        bool p = n >= 2;
        for (long i = 2; i * i <= n; i++)
            if (n % i == 0) { p = false; break; }
        Console.WriteLine(p ? "yes" : "no");
    }
}
""",
        },
    },
    {
        "slug": "to-uppercase",
        "title": "Uppercase",
        "desc": "Read one line and print it with every ASCII letter converted to uppercase. Other characters are unchanged.",
        "stdin": "a single line of text",
        "stdout": "the line uppercased",
        "cases": [("hello\n", "HELLO"), ("Hello World\n", "HELLO WORLD"),
                  ("abc123\n", "ABC123"), ("MixED\n", "MIXED"), ("xyz\n", "XYZ")],
        "refs": {
            "python": r"""import sys
line = sys.stdin.readline().rstrip('\n').rstrip('\r')
print(line.upper())
""",
            "rust": r"""use std::io::{self, BufRead};
fn main() {
    let mut s = String::new();
    io::stdin().lock().read_line(&mut s).unwrap();
    let s = s.trim_end_matches(['\n', '\r']);
    println!("{}", s.to_ascii_uppercase());
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "strings"
)

func main() {
    r := bufio.NewReader(os.Stdin)
    line, _ := r.ReadString('\n')
    line = strings.TrimRight(line, "\r\n")
    fmt.Println(strings.ToUpper(line))
}
""",
            "c": r"""#include <stdio.h>
#include <string.h>
#include <ctype.h>
int main(void) {
    char buf[100000];
    if (!fgets(buf, sizeof buf, stdin)) buf[0] = 0;
    size_t n = strlen(buf);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = 0;
    for (size_t i = 0; i < n; i++) buf[i] = toupper((unsigned char)buf[i]);
    printf("%s\n", buf);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <cctype>
int main() {
    std::string s;
    std::getline(std::cin, s);
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    for (char &c : s) c = (char)std::toupper((unsigned char)c);
    std::cout << s << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        var line = Console.ReadLine() ?? "";
        Console.WriteLine(line.ToUpperInvariant());
    }
}
""",
        },
    },
    {
        "slug": "second-largest",
        "title": "Second largest distinct",
        "desc": "Read a list of integers (at least two distinct values) and print the second-largest distinct value.",
        "stdin": "whitespace-separated integers",
        "stdout": "a single integer: the second-largest distinct value",
        "cases": [("3 1 4 1 5\n", "4"), ("10 20 30\n", "20"), ("5 5 4\n", "4"),
                  ("-1 -2 -3\n", "-2"), ("7 7 8 8\n", "7")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sorted(set(nums), reverse=True)[1])
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    v.sort();
    v.dedup();
    println!("{}", v[v.len() - 2]);
}
""",
            "go": r"""package main

import (
    "fmt"
    "sort"
)

func main() {
    var x int
    seen := map[int]bool{}
    var v []int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if !seen[x] {
            seen[x] = true
            v = append(v, x)
        }
    }
    sort.Ints(v)
    fmt.Println(v[len(v)-2])
}
""",
            "c": r"""#include <stdio.h>
#include <limits.h>
int main(void) {
    long long x, max1 = LLONG_MIN, max2 = LLONG_MIN;
    int any = 0;
    long long arr[100000];
    int n = 0;
    while (scanf("%lld", &x) == 1 && n < 100000) arr[n++] = x;
    for (int i = 0; i < n; i++) if (arr[i] > max1) max1 = arr[i];
    for (int i = 0; i < n; i++)
        if (arr[i] < max1 && arr[i] > max2) { max2 = arr[i]; any = 1; }
    (void)any;
    printf("%lld\n", max2);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <vector>
#include <algorithm>
int main() {
    long long x;
    std::vector<long long> v;
    while (std::cin >> x) v.push_back(x);
    std::sort(v.begin(), v.end());
    v.erase(std::unique(v.begin(), v.end()), v.end());
    std::cout << v[v.size() - 2] << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Distinct().OrderByDescending(z => z).ToArray();
        Console.WriteLine(v[1]);
    }
}
""",
        },
    },
    {
        "slug": "sort-ascending",
        "title": "Sort ascending",
        "desc": "Read a list of integers and print them sorted in ascending order, separated by single spaces, on one line.",
        "stdin": "whitespace-separated integers",
        "stdout": "the integers in ascending order, space-separated",
        "cases": [("3 1 2\n", "1 2 3"), ("5\n", "5"), ("-1 -3 -2\n", "-3 -2 -1"),
                  ("10 10 1\n", "1 10 10"), ("0 -5 5\n", "-5 0 5")],
        "refs": {
            "python": r"""import sys
nums = sorted(int(x) for x in sys.stdin.read().split())
print(" ".join(str(x) for x in nums))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    v.sort();
    let parts: Vec<String> = v.iter().map(|x| x.to_string()).collect();
    println!("{}", parts.join(" "));
}
""",
            "go": r"""package main

import (
    "fmt"
    "sort"
)

func main() {
    var x int
    var v []int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        v = append(v, x)
    }
    sort.Ints(v)
    for i, val := range v {
        if i > 0 {
            fmt.Print(" ")
        }
        fmt.Print(val)
    }
    fmt.Println()
}
""",
            "c": r"""#include <stdio.h>
#include <stdlib.h>
static int cmp(const void *a, const void *b) {
    long long x = *(const long long *)a, y = *(const long long *)b;
    return (x > y) - (x < y);
}
int main(void) {
    long long arr[100000];
    int n = 0;
    while (n < 100000 && scanf("%lld", &arr[n]) == 1) n++;
    qsort(arr, n, sizeof(long long), cmp);
    for (int i = 0; i < n; i++) printf(i ? " %lld" : "%lld", arr[i]);
    printf("\n");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <vector>
#include <algorithm>
int main() {
    long long x;
    std::vector<long long> v;
    while (std::cin >> x) v.push_back(x);
    std::sort(v.begin(), v.end());
    for (size_t i = 0; i < v.size(); i++) {
        if (i) std::cout << ' ';
        std::cout << v[i];
    }
    std::cout << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).OrderBy(z => z);
        Console.WriteLine(string.Join(" ", v));
    }
}
""",
        },
    },
    {
        "slug": "dedupe-order",
        "title": "Deduplicate preserving order",
        "desc": "Read a list of integers and print them with duplicates removed, keeping the first occurrence of each value, separated by single spaces.",
        "stdin": "whitespace-separated integers",
        "stdout": "the de-duplicated integers, space-separated, in first-seen order",
        "cases": [("1 2 1 3 2\n", "1 2 3"), ("5 5 5\n", "5"), ("1 2 3\n", "1 2 3"),
                  ("3 1 3 2 1\n", "3 1 2"), ("7\n", "7")],
        "refs": {
            "python": r"""import sys
seen = set()
out = []
for x in sys.stdin.read().split():
    if x not in seen:
        seen.add(x)
        out.append(x)
print(" ".join(out))
""",
            "rust": r"""use std::collections::HashSet;
use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut seen = HashSet::new();
    let mut out: Vec<String> = Vec::new();
    for t in s.split_whitespace() {
        if seen.insert(t.to_string()) {
            out.push(t.to_string());
        }
    }
    println!("{}", out.join(" "));
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x int
    seen := map[int]bool{}
    var out []int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if !seen[x] {
            seen[x] = true
            out = append(out, x)
        }
    }
    for i, v := range out {
        if i > 0 {
            fmt.Print(" ")
        }
        fmt.Print(v)
    }
    fmt.Println()
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long arr[100000];
    int n = 0;
    long long x;
    int first = 1;
    while (n < 100000 && scanf("%lld", &x) == 1) {
        int dup = 0;
        for (int i = 0; i < n; i++) if (arr[i] == x) { dup = 1; break; }
        if (!dup) {
            arr[n++] = x;
            printf(first ? "%lld" : " %lld", x);
            first = 0;
        }
    }
    printf("\n");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <unordered_set>
int main() {
    long long x;
    std::unordered_set<long long> seen;
    bool first = true;
    while (std::cin >> x) {
        if (seen.insert(x).second) {
            if (!first) std::cout << ' ';
            std::cout << x;
            first = false;
        }
    }
    std::cout << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Distinct();
        Console.WriteLine(string.Join(" ", v));
    }
}
""",
        },
    },
    {
        "slug": "binary-to-decimal",
        "title": "Binary to decimal",
        "desc": "Read a string of 0s and 1s and print its value as a base-10 integer.",
        "stdin": "a single binary string",
        "stdout": "a single integer: the decimal value",
        "cases": [("101\n", "5"), ("0\n", "0"), ("1111\n", "15"),
                  ("1000\n", "8"), ("100000\n", "32")],
        "refs": {
            "python": r"""import sys
print(int(sys.stdin.read().split()[0], 2))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let tok = s.split_whitespace().next().unwrap();
    println!("{}", i64::from_str_radix(tok, 2).unwrap());
}
""",
            "go": r"""package main

import (
    "fmt"
    "strconv"
)

func main() {
    var tok string
    fmt.Scan(&tok)
    v, _ := strconv.ParseInt(tok, 2, 64)
    fmt.Println(v)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    char buf[1 << 16];
    if (scanf("%65535s", buf) != 1) { printf("0\n"); return 0; }
    long long v = 0;
    for (int i = 0; buf[i]; i++) v = v * 2 + (buf[i] - '0');
    printf("%lld\n", v);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
int main() {
    std::string tok;
    std::cin >> tok;
    long long v = 0;
    for (char c : tok) v = v * 2 + (c - '0');
    std::cout << v << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        var tok = Console.In.ReadToEnd().Trim().Split()[0];
        Console.WriteLine(Convert.ToInt64(tok, 2));
    }
}
""",
        },
    },
    {
        "slug": "decimal-to-binary",
        "title": "Decimal to binary",
        "desc": "Read a non-negative integer and print its binary representation, with no leading zeros (0 prints as \"0\").",
        "stdin": "a single non-negative integer",
        "stdout": "the binary representation",
        "cases": [("5\n", "101"), ("0\n", "0"), ("8\n", "1000"),
                  ("255\n", "11111111"), ("1\n", "1")],
        "refs": {
            "python": r"""import sys
n = int(sys.stdin.read().split()[0])
print(format(n, 'b'))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n: u64 = s.split_whitespace().next().unwrap().parse().unwrap();
    println!("{:b}", n);
}
""",
            "go": r"""package main

import (
    "fmt"
    "strconv"
)

func main() {
    var n int64
    fmt.Scan(&n)
    fmt.Println(strconv.FormatInt(n, 2))
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    unsigned long long n;
    if (scanf("%llu", &n) != 1) return 0;
    if (n == 0) { printf("0\n"); return 0; }
    char buf[70];
    int i = 0;
    while (n > 0) { buf[i++] = '0' + (n & 1); n >>= 1; }
    while (i > 0) putchar(buf[--i]);
    putchar('\n');
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
int main() {
    unsigned long long n;
    std::cin >> n;
    if (n == 0) { std::cout << "0" << std::endl; return 0; }
    std::string out;
    while (n > 0) { out += char('0' + (n & 1)); n >>= 1; }
    std::string rev(out.rbegin(), out.rend());
    std::cout << rev << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        long n = long.Parse(Console.In.ReadToEnd().Trim().Split()[0]);
        Console.WriteLine(Convert.ToString(n, 2));
    }
}
""",
        },
    },
    {
        "slug": "power",
        "title": "Integer power",
        "desc": "Read two non-negative integers, base and exponent, and print base raised to that exponent.",
        "stdin": "two whitespace-separated integers: base and exponent (exponent ≥ 0)",
        "stdout": "a single integer: base ** exponent",
        "cases": [("2 10\n", "1024"), ("3 0\n", "1"), ("5 3\n", "125"),
                  ("10 2\n", "100"), ("2 0\n", "1")],
        "refs": {
            "python": r"""import sys
a, b = (int(x) for x in sys.stdin.read().split()[:2])
print(a ** b)
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    let mut r: i64 = 1;
    for _ in 0..v[1] {
        r *= v[0];
    }
    println!("{}", r);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var a, b int64
    fmt.Scan(&a, &b)
    r := int64(1)
    for i := int64(0); i < b; i++ {
        r *= a
    }
    fmt.Println(r)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long a, b;
    if (scanf("%lld %lld", &a, &b) != 2) return 0;
    long long r = 1;
    for (long long i = 0; i < b; i++) r *= a;
    printf("%lld\n", r);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long a, b;
    std::cin >> a >> b;
    long long r = 1;
    for (long long i = 0; i < b; i++) r *= a;
    std::cout << r << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
class Sol {
    static void Main() {
        var p = Console.In.ReadToEnd().Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        long a = long.Parse(p[0]), b = long.Parse(p[1]);
        long r = 1;
        for (long i = 0; i < b; i++) r *= a;
        Console.WriteLine(r);
    }
}
""",
        },
    },
    {
        "slug": "sum-even",
        "title": "Sum of even numbers",
        "desc": "Read a list of integers and print the sum of the even ones.",
        "stdin": "whitespace-separated integers",
        "stdout": "a single integer: the sum of the even values (0 if none)",
        "cases": [("1 2 3 4\n", "6"), ("2 4 6\n", "12"), ("1 3 5\n", "0"),
                  ("-2 -4 3\n", "-6"), ("0 1 2\n", "2")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sum(x for x in nums if x % 2 == 0))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let sum: i64 = s.split_whitespace()
        .map(|x| x.parse::<i64>().unwrap())
        .filter(|x| x % 2 == 0)
        .sum();
    println!("{}", sum);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x, sum int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if x%2 == 0 {
            sum += x
        }
    }
    fmt.Println(sum)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long x, sum = 0;
    while (scanf("%lld", &x) == 1)
        if (x % 2 == 0) sum += x;
    printf("%lld\n", sum);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long x, sum = 0;
    while (std::cin >> x)
        if (x % 2 == 0) sum += x;
    std::cout << sum << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var sum = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Where(x => x % 2 == 0).Sum();
        Console.WriteLine(sum);
    }
}
""",
        },
    },
    {
        "slug": "longest-word",
        "title": "Longest word",
        "desc": "Read a line of whitespace-separated words and print the longest one. If several share the maximum length, print the first of them.",
        "stdin": "a line of words separated by spaces",
        "stdout": "the longest word",
        "cases": [("the quick brown fox\n", "quick"), ("a bb ccc\n", "ccc"),
                  ("hello\n", "hello"), ("one two six\n", "one"), ("ab cd ef\n", "ab")],
        "refs": {
            "python": r"""import sys
best = ""
for w in sys.stdin.read().split():
    if len(w) > len(best):
        best = w
print(best)
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut best = "";
    for w in s.split_whitespace() {
        if w.len() > best.len() {
            best = w;
        }
    }
    println!("{}", best);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var w, best string
    for {
        if _, err := fmt.Scan(&w); err != nil {
            break
        }
        if len(w) > len(best) {
            best = w
        }
    }
    fmt.Println(best)
}
""",
            "c": r"""#include <stdio.h>
#include <string.h>
int main(void) {
    char w[65536], best[65536];
    best[0] = 0;
    while (scanf("%65535s", w) == 1)
        if (strlen(w) > strlen(best)) strcpy(best, w);
    printf("%s\n", best);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
int main() {
    std::string w, best;
    while (std::cin >> w)
        if (w.size() > best.size()) best = w;
    std::cout << best << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var words = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        string best = "";
        foreach (var w in words)
            if (w.Length > best.Length) best = w;
        Console.WriteLine(best);
    }
}
""",
        },
    },
    {
        "slug": "title-case",
        "title": "Title case",
        "desc": "Read a line of words and print each word with its first letter uppercased and the remaining letters lowercased, separated by single spaces.",
        "stdin": "a line of words separated by spaces",
        "stdout": "the words in title case, single-spaced",
        "cases": [("hello world\n", "Hello World"), ("the QUICK fox\n", "The Quick Fox"),
                  ("abc def\n", "Abc Def"), ("HELLO\n", "Hello"),
                  ("mixED cASE\n", "Mixed Case")],
        "refs": {
            "python": r"""import sys
words = sys.stdin.read().split()
print(" ".join(w[0].upper() + w[1:].lower() for w in words))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let parts: Vec<String> = s.split_whitespace().map(|w| {
        let mut c = w.chars();
        let first = c.next().unwrap().to_ascii_uppercase();
        format!("{}{}", first, c.as_str().to_ascii_lowercase())
    }).collect();
    println!("{}", parts.join(" "));
}
""",
            "go": r"""package main

import (
    "fmt"
    "strings"
)

func main() {
    var w string
    first := true
    for {
        if _, err := fmt.Scan(&w); err != nil {
            break
        }
        if !first {
            fmt.Print(" ")
        }
        first = false
        fmt.Print(strings.ToUpper(w[:1]) + strings.ToLower(w[1:]))
    }
    fmt.Println()
}
""",
            "c": r"""#include <stdio.h>
#include <ctype.h>
int main(void) {
    char w[65536];
    int first = 1;
    while (scanf("%65535s", w) == 1) {
        if (!first) putchar(' ');
        first = 0;
        for (int i = 0; w[i]; i++) {
            int ch = (i == 0) ? toupper((unsigned char)w[i])
                              : tolower((unsigned char)w[i]);
            putchar(ch);
        }
    }
    putchar('\n');
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <cctype>
int main() {
    std::string w;
    bool first = true;
    while (std::cin >> w) {
        if (!first) std::cout << ' ';
        first = false;
        for (size_t i = 0; i < w.size(); i++) {
            unsigned char c = (unsigned char)w[i];
            std::cout << (char)(i == 0 ? std::toupper(c) : std::tolower(c));
        }
    }
    std::cout << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var words = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(w => char.ToUpperInvariant(w[0]) + w.Substring(1).ToLowerInvariant());
        Console.WriteLine(string.Join(" ", words));
    }
}
""",
        },
    },
    {
        "slug": "count-evens",
        "title": "Count even numbers",
        "desc": "Read a list of integers and print how many of them are even.",
        "stdin": "whitespace-separated integers",
        "stdout": "a single integer: the count of even values",
        "cases": [("1 2 3 4\n", "2"), ("2 4 6\n", "3"), ("1 3 5\n", "0"),
                  ("-2 0 2\n", "3"), ("7\n", "0")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sum(1 for x in nums if x % 2 == 0))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let n = s.split_whitespace()
        .map(|x| x.parse::<i64>().unwrap())
        .filter(|x| x % 2 == 0)
        .count();
    println!("{}", n);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x, n int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if x%2 == 0 {
            n++
        }
    }
    fmt.Println(n)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long x;
    int n = 0;
    while (scanf("%lld", &x) == 1)
        if (x % 2 == 0) n++;
    printf("%d\n", n);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long x;
    int n = 0;
    while (std::cin >> x)
        if (x % 2 == 0) n++;
    std::cout << n << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var n = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Count(x => x % 2 == 0);
        Console.WriteLine(n);
    }
}
""",
        },
    },
    {
        "slug": "prefix-sums",
        "title": "Prefix sums",
        "desc": "Read a list of integers and print their running (prefix) sums, separated by single spaces. The i-th output is the sum of the first i inputs.",
        "stdin": "whitespace-separated integers",
        "stdout": "the prefix sums, space-separated",
        "cases": [("1 2 3\n", "1 3 6"), ("5\n", "5"), ("1 1 1 1\n", "1 2 3 4"),
                  ("-1 1 -1\n", "-1 0 -1"), ("10 20\n", "10 30")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
acc = 0
out = []
for x in nums:
    acc += x
    out.append(str(acc))
print(" ".join(out))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut acc: i64 = 0;
    let parts: Vec<String> = s.split_whitespace().map(|x| {
        acc += x.parse::<i64>().unwrap();
        acc.to_string()
    }).collect();
    println!("{}", parts.join(" "));
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x, acc int
    first := true
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        acc += x
        if !first {
            fmt.Print(" ")
        }
        first = false
        fmt.Print(acc)
    }
    fmt.Println()
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long x, acc = 0;
    int first = 1;
    while (scanf("%lld", &x) == 1) {
        acc += x;
        printf(first ? "%lld" : " %lld", acc);
        first = 0;
    }
    printf("\n");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long x, acc = 0;
    bool first = true;
    while (std::cin >> x) {
        acc += x;
        if (!first) std::cout << ' ';
        first = false;
        std::cout << acc;
    }
    std::cout << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Collections.Generic;
using System.Linq;
class Sol {
    static void Main() {
        long acc = 0;
        var outp = new List<string>();
        foreach (var t in Console.In.ReadToEnd()
                 .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)) {
            acc += long.Parse(t);
            outp.Add(acc.ToString());
        }
        Console.WriteLine(string.Join(" ", outp));
    }
}
""",
        },
    },
    {
        "slug": "is-anagram",
        "title": "Anagram check",
        "desc": "Read two lines and print \"yes\" if the second is an anagram of the first (same multiset of characters), otherwise \"no\". Comparison is exact and case-sensitive.",
        "stdin": "two lines: the first string, then the second string",
        "stdout": "\"yes\" or \"no\"",
        "cases": [("listen\nsilent\n", "yes"), ("abc\ncba\n", "yes"),
                  ("abc\nabd\n", "no"), ("aabb\nbbaa\n", "yes"), ("a\nb\n", "no")],
        "refs": {
            "python": r"""import sys
parts = sys.stdin.read().split('\n')
a = parts[0].rstrip('\r')
b = parts[1].rstrip('\r') if len(parts) > 1 else ''
print("yes" if sorted(a) == sorted(b) else "no")
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut lines = s.lines();
    let a = lines.next().unwrap_or("");
    let b = lines.next().unwrap_or("");
    let mut ca: Vec<char> = a.chars().collect();
    let mut cb: Vec<char> = b.chars().collect();
    ca.sort();
    cb.sort();
    println!("{}", if ca == cb { "yes" } else { "no" });
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "sort"
)

func main() {
    sc := bufio.NewScanner(os.Stdin)
    sc.Buffer(make([]byte, 1024*1024), 1024*1024)
    sc.Scan()
    a := []byte(sc.Text())
    sc.Scan()
    b := []byte(sc.Text())
    sort.Slice(a, func(i, j int) bool { return a[i] < a[j] })
    sort.Slice(b, func(i, j int) bool { return b[i] < b[j] })
    if string(a) == string(b) {
        fmt.Println("yes")
    } else {
        fmt.Println("no")
    }
}
""",
            "c": r"""#include <stdio.h>
#include <string.h>
#include <stdlib.h>
static int cmpc(const void *a, const void *b) {
    return (int)(*(const unsigned char *)a) - (int)(*(const unsigned char *)b);
}
static size_t rd(char *buf) {
    if (!fgets(buf, 100000, stdin)) buf[0] = 0;
    size_t n = strlen(buf);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = 0;
    return n;
}
int main(void) {
    char a[100000], b[100000];
    size_t na = rd(a), nb = rd(b);
    qsort(a, na, 1, cmpc);
    qsort(b, nb, 1, cmpc);
    printf("%s\n", (na == nb && memcmp(a, b, na) == 0) ? "yes" : "no");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <algorithm>
static std::string rd() {
    std::string s;
    std::getline(std::cin, s);
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    return s;
}
int main() {
    std::string a = rd(), b = rd();
    std::sort(a.begin(), a.end());
    std::sort(b.begin(), b.end());
    std::cout << (a == b ? "yes" : "no") << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var a = (Console.ReadLine() ?? "").OrderBy(c => c);
        var b = (Console.ReadLine() ?? "").OrderBy(c => c);
        Console.WriteLine(a.SequenceEqual(b) ? "yes" : "no");
    }
}
""",
        },
    },
    {
        "slug": "median-odd",
        "title": "Median (odd count)",
        "desc": "Read an odd-length list of integers and print the median (the middle value of the sorted list).",
        "stdin": "whitespace-separated integers (an odd count)",
        "stdout": "a single integer: the median",
        "cases": [("3 1 2\n", "2"), ("5\n", "5"), ("9 1 5 3 7\n", "5"),
                  ("-1 -3 -2\n", "-2"), ("10 20 30 40 50\n", "30")],
        "refs": {
            "python": r"""import sys
nums = sorted(int(x) for x in sys.stdin.read().split())
print(nums[len(nums) // 2])
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut v: Vec<i64> = s.split_whitespace().map(|x| x.parse().unwrap()).collect();
    v.sort();
    println!("{}", v[v.len() / 2]);
}
""",
            "go": r"""package main

import (
    "fmt"
    "sort"
)

func main() {
    var x int
    var v []int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        v = append(v, x)
    }
    sort.Ints(v)
    fmt.Println(v[len(v)/2])
}
""",
            "c": r"""#include <stdio.h>
#include <stdlib.h>
static int cmp(const void *a, const void *b) {
    long long x = *(const long long *)a, y = *(const long long *)b;
    return (x > y) - (x < y);
}
int main(void) {
    long long arr[100000];
    int n = 0;
    while (n < 100000 && scanf("%lld", &arr[n]) == 1) n++;
    qsort(arr, n, sizeof(long long), cmp);
    printf("%lld\n", arr[n / 2]);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <vector>
#include <algorithm>
int main() {
    long long x;
    std::vector<long long> v;
    while (std::cin >> x) v.push_back(x);
    std::sort(v.begin(), v.end());
    std::cout << v[v.size() / 2] << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var v = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).OrderBy(z => z).ToArray();
        Console.WriteLine(v[v.Length / 2]);
    }
}
""",
        },
    },
    {
        "slug": "sum-of-squares",
        "title": "Sum of squares",
        "desc": "Read a list of integers and print the sum of their squares.",
        "stdin": "whitespace-separated integers",
        "stdout": "a single integer: the sum of squares",
        "cases": [("1 2 3\n", "14"), ("2\n", "4"), ("-1 -2\n", "5"),
                  ("0 0\n", "0"), ("3 4\n", "25")],
        "refs": {
            "python": r"""import sys
nums = [int(x) for x in sys.stdin.read().split()]
print(sum(x * x for x in nums))
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let sum: i64 = s.split_whitespace()
        .map(|x| x.parse::<i64>().unwrap())
        .map(|x| x * x)
        .sum();
    println!("{}", sum);
}
""",
            "go": r"""package main

import "fmt"

func main() {
    var x, sum int64
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        sum += x * x
    }
    fmt.Println(sum)
}
""",
            "c": r"""#include <stdio.h>
int main(void) {
    long long x, sum = 0;
    while (scanf("%lld", &x) == 1) sum += x * x;
    printf("%lld\n", sum);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main() {
    long long x, sum = 0;
    while (std::cin >> x) sum += x * x;
    std::cout << sum << std::endl;
    return 0;
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var sum = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).Sum(x => x * x);
        Console.WriteLine(sum);
    }
}
""",
        },
    },
]


# --------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------- #
ORACLE_TEMPLATE = '''\
"""Oracle for @@ID@@ — AUTO-GENERATED by gen_coder_easy.py; do not hand-edit."""

import os
import sys
from pathlib import Path

import pytest

_HARNESS_ROOT = Path(__file__).resolve().parents[4]
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from benchmarks.consultants.oracles_mlang import (  # noqa: E402
    CompileError,
    run_program,
)

SANDBOX = Path(os.environ["CODER_SANDBOX"])
SOURCE = SANDBOX / "@@SANDBOX@@"
LANG = "@@LANG@@"
CASES = @@CASES@@


def _run(stdin_input):
    try:
        return run_program(
            lang=LANG, source=SOURCE, stdin_input=stdin_input, timeout_s=10,
        )
    except CompileError as e:
        raise AssertionError(
            f"@@LANG@@ toolchain rejected @@SANDBOX@@:\\n{e.stderr}"
        ) from None


@pytest.mark.constraint
def test_source_present():
    assert SOURCE.is_file(), "model did not write @@SANDBOX@@"


@pytest.mark.parametrize("stdin_input,expected", CASES)
def test_io(stdin_input, expected):
    rc, out, err = _run(stdin_input)
    assert rc == 0, f"non-zero exit {rc}\\nstderr:\\n{err}"
    assert out.strip() == expected.strip(), (
        f"stdin={stdin_input!r} expected={expected!r} "
        f"got={out!r} stderr={err!r}"
    )
'''


def _task_block(problem: dict, lang: str, ext: str) -> str:
    """The ``task: |`` block scalar — every line indented 2 spaces."""
    lines = [
        f"Write a {DISPLAY[lang]} program in a file named "
        f"`solution{ext}` that reads from standard input and writes "
        f"the answer to standard output.",
        "",
        problem["desc"],
        "",
        f"Input: {problem['stdin']}.",
        f"Output: {problem['stdout']}.",
        "",
        "Examples (stdin -> stdout):",
    ]
    for stdin, expected in problem["cases"]:
        lines.append(f"  {stdin!r} -> {expected!r}")
    return "\n".join("  " + ln if ln else "" for ln in lines)


def _examples_md(problem: dict) -> str:
    rows = ["| stdin | stdout |", "|---|---|"]
    for stdin, expected in problem["cases"]:
        rows.append(f"| `{stdin!r}` | `{expected!r}` |")
    return "\n".join(rows)


def _task_md(problem: dict, lang: str, qid: str, ext: str) -> str:
    return (
        "---\n"
        f"id: {qid}\n"
        "tier: easy\n"
        f"source: coder_easy/{problem['slug']}\n"
        f"sandbox_path: solution{ext}\n"
        f"oracle: {qid}-oracle.py\n"
        f"language: {lang}\n"
        "task: |\n"
        f"{_task_block(problem, lang, ext)}\n"
        "notes: |\n"
        "  Auto-generated stdin/stdout easy problem (suite coder_easy).\n"
        "  Uniform contract across all six languages.\n"
        "---\n\n"
        f"# {qid} — {problem['title']}\n\n"
        f"{problem['desc']}\n\n"
        f"- **Input:** {problem['stdin']}\n"
        f"- **Output:** {problem['stdout']}\n\n"
        "## Examples\n\n"
        f"{_examples_md(problem)}\n"
    )


def _oracle_py(qid: str, lang: str, ext: str, cases: list) -> str:
    return (
        ORACLE_TEMPLATE
        .replace("@@ID@@", qid)
        .replace("@@SANDBOX@@", f"solution{ext}")
        .replace("@@LANG@@", lang)
        .replace("@@CASES@@", repr(cases))
    )


CONFTEST = '''\
"""pytest config for the coder_easy oracle suite (AUTO-GENERATED).

Registers the ``constraint`` marker so the two-axis grading in
coder_bench treats ``test_source_present`` as instruction-following
(does NOT gate ``passes_algorithm``) while the parametrized ``test_io``
cases decide correctness.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "constraint: structural check; does NOT gate trial.passes_algorithm",
    )
'''


def _suite_md(ids: list[str]) -> str:
    manifest = "\n".join(f"  - {i}" for i in ids)
    body_hash = hashlib.sha256(
        "\n".join(ids).encode("utf-8")
    ).hexdigest()[:12]
    return (
        "---\n"
        "suite: coder_easy\n"
        f'suite_version: "{SUITE_VERSION}"\n'
        "released: 2026-06-03\n"
        f"suite_hash: {body_hash}\n"
        "manifest:\n"
        f"{manifest}\n"
        "rubric:\n"
        "  pass_rate_floor: 0.70\n"
        "  quality_score_floor: 3.5\n"
        "  tie_breaker: median_tokens\n"
        "---\n\n"
        "# Coder Easy Skill-Eval Suite v1.0\n\n"
        "Multi-language **easy** coder suite — the discriminating-at-the-"
        "easy-tier counterpart to `coder_mlang` (which saturated at *too "
        "hard*). 30 problems × 6 languages = **180 questions**, one uniform "
        "contract: read stdin, write stdout.\n\n"
        "AUTO-GENERATED by `benchmarks/consultants/gen_coder_easy.py` — edit "
        "the spec there and regenerate; do not hand-edit question files.\n\n"
        "## Rubric\n\n"
        "A model **qualifies** iff `pass_rate ≥ 0.70` AND "
        "`avg_quality ≥ 3.5`. Among qualifying models the recommended "
        "default is the highest pass-rate; ties break on `median_tokens`. "
        "Per-language winners are derived the same way over each language's "
        "30 questions.\n\n"
        "## Languages\n\n"
        "Python, Rust, Go, C, C++, C# — toolchains pinned in run metadata "
        "via `oracles_mlang.probe_toolchain_versions()`.\n"
    )


def main() -> None:
    SUITE_DIR.mkdir(parents=True, exist_ok=True)
    # Clean previously-generated artifacts so removed problems don't linger.
    for old in SUITE_DIR.glob("*-easy-*"):
        old.unlink()
    ids: list[str] = []
    n_files = 0
    for idx, problem in enumerate(PROBLEMS, start=1):
        nn = f"{idx:02d}"
        for lang, ext in LANGS.items():
            qid = f"{lang}-easy-{nn}-{problem['slug']}"
            ids.append(qid)
            (SUITE_DIR / f"{qid}.md").write_text(
                _task_md(problem, lang, qid, ext), encoding="utf-8")
            (SUITE_DIR / f"{qid}-oracle.py").write_text(
                _oracle_py(qid, lang, ext, problem["cases"]), encoding="utf-8")
            (SUITE_DIR / f"{qid}.ref{ext}").write_text(
                problem["refs"][lang], encoding="utf-8")
            n_files += 3
    (SUITE_DIR / "SUITE.md").write_text(_suite_md(ids), encoding="utf-8")
    (SUITE_DIR / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    print(
        f"generated {len(PROBLEMS)} problems × {len(LANGS)} languages = "
        f"{len(ids)} questions ({n_files} artifacts) -> {SUITE_DIR}"
    )


if __name__ == "__main__":
    main()
