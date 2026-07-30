#!/usr/bin/env python3
"""Generate the ``coder_med`` skill-eval suite — 10 easy-medium problems
× 6 languages = 60 questions, one uniform stdin->stdout contract.

This is the discrimination band *between* ``coder_easy`` (which saturates —
every strong model passes ~100%) and ``coder_mlang`` (which defeats every
model). The problems require real algorithmic thinking + edge-case handling
(RLE, bracket matching, roman numerals, interval merging, operator
precedence, spiral traversal, Kadane, frequency tie-breaks, base conversion,
sliding-window max) so careless implementations fail on the edge cases.

Each (problem × language) emits three artifacts under
``questions/coder_med/``:
  - ``<id>.md``          task (frontmatter + body)
  - ``<id>-oracle.py``   pytest oracle (two-axis: constraint + parametrized IO)
  - ``<id>.ref<ext>``    a correct reference solution (dry-run fallback)

plus ``SUITE.md`` (manifest + rubric, hash via harness._hash_for_suite) and
``conftest.py`` (the ``constraint`` marker). Re-run after editing the spec:

    python benchmarks/consultants/gen_coder_med.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo-root-aware import so the recorded SUITE.md hash is computed by the
# SAME function the runtime uses (harness._hash_for_suite).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.harness import _hash_for_suite  # noqa: E402

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
    Path(__file__).resolve().parent / "questions" / "coder_med"
)
SUITE_VERSION = "1.0"


# --------------------------------------------------------------------- #
# Problem spec — see gen_coder_easy.py for the field contract. The oracle
# compares ``stdout.strip() == expected.strip()`` so outputs are single-line
# where possible and never carry meaningful leading/trailing whitespace.
# --------------------------------------------------------------------- #
PROBLEMS: list[dict] = [
    # 1 -------------------------------------------------------------- #
    {
        "slug": "rle-encode",
        "title": "Run-length encode",
        "desc": ("Run-length encode a string: replace each maximal run of "
                 "one repeated character with that character followed by the "
                 "length of the run. Runs reset when the character changes."),
        "stdin": "one line of lowercase letters (no spaces)",
        "stdout": "the run-length encoding on one line",
        "cases": [
            ("aaabbc\n", "a3b2c1"),
            ("abc\n", "a1b1c1"),
            ("aaaa\n", "a4"),
            ("a\n", "a1"),
            ("aabbaa\n", "a2b2a2"),
            ("xyyz\n", "x1y2z1"),
        ],
        "refs": {
            "python": r"""import sys
s = sys.stdin.readline().rstrip("\n").rstrip("\r")
out = []
i, n = 0, len(s)
while i < n:
    j = i
    while j < n and s[j] == s[i]:
        j += 1
    out.append(s[i] + str(j - i))
    i = j
print("".join(out))
""",
            "c": r"""#include <stdio.h>
#include <string.h>
int main(void){
    static char s[1000000];
    if(!fgets(s, sizeof(s), stdin)) return 0;
    int n = (int)strlen(s);
    while(n>0 && (s[n-1]=='\n'||s[n-1]=='\r')) s[--n]=0;
    int i=0;
    while(i<n){
        int j=i;
        while(j<n && s[j]==s[i]) j++;
        printf("%c%d", s[i], j-i);
        i=j;
    }
    printf("\n");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
int main(){
    std::string s;
    std::getline(std::cin, s);
    while(!s.empty() && (s.back()=='\r'||s.back()=='\n')) s.pop_back();
    std::string out;
    size_t i=0, n=s.size();
    while(i<n){
        size_t j=i;
        while(j<n && s[j]==s[i]) j++;
        out += s[i];
        out += std::to_string((int)(j-i));
        i=j;
    }
    std::cout << out << "\n";
    return 0;
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "strconv"
    "strings"
)

func main() {
    r := bufio.NewReader(os.Stdin)
    line, _ := r.ReadString('\n')
    line = strings.TrimRight(line, "\r\n")
    var b strings.Builder
    n := len(line)
    i := 0
    for i < n {
        j := i
        for j < n && line[j] == line[i] {
            j++
        }
        b.WriteByte(line[i])
        b.WriteString(strconv.Itoa(j - i))
        i = j
    }
    fmt.Println(b.String())
}
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim_end();
    let b = s.as_bytes();
    let n = b.len();
    let mut i = 0;
    let mut out = String::new();
    while i < n {
        let mut j = i;
        while j < n && b[j] == b[i] {
            j += 1;
        }
        out.push(b[i] as char);
        out.push_str(&(j - i).to_string());
        i = j;
    }
    println!("{}", out);
}
""",
            "csharp": r"""using System;
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
""",
        },
    },
    # 2 -------------------------------------------------------------- #
    {
        "slug": "balanced-brackets",
        "title": "Balanced brackets",
        "desc": ("Decide whether a string of brackets is balanced. The three "
                 "bracket pairs are (), [], and {}. Every opening bracket must "
                 "be closed by the matching kind in the correct order."),
        "stdin": "one line containing only the characters ()[]{} (possibly empty)",
        "stdout": "YES if balanced, otherwise NO",
        "cases": [
            ("()[]{}\n", "YES"),
            ("([{}])\n", "YES"),
            ("([)]\n", "NO"),
            ("(((\n", "NO"),
            (")(\n", "NO"),
            ("\n", "YES"),
            ("{[()()]}\n", "YES"),
        ],
        "refs": {
            "python": r"""import sys
s = sys.stdin.readline().rstrip("\n").rstrip("\r")
pairs = {")": "(", "]": "[", "}": "{"}
st = []
ok = True
for c in s:
    if c in "([{":
        st.append(c)
    elif c in pairs:
        if not st or st.pop() != pairs[c]:
            ok = False
            break
print("YES" if ok and not st else "NO")
""",
            "c": r"""#include <stdio.h>
#include <string.h>
int main(void){
    static char s[1000000];
    if(!fgets(s, sizeof(s), stdin)){ printf("YES\n"); return 0; }
    int n=(int)strlen(s);
    while(n>0 && (s[n-1]=='\n'||s[n-1]=='\r')) s[--n]=0;
    static char st[1000000];
    int top=0, ok=1;
    for(int i=0;i<n;i++){
        char c=s[i];
        if(c=='('||c=='['||c=='{') st[top++]=c;
        else if(c==')'||c==']'||c=='}'){
            char m = c==')'?'(':(c==']'?'[':'{');
            if(top==0 || st[--top]!=m){ ok=0; break; }
        }
    }
    printf("%s\n", (ok && top==0) ? "YES" : "NO");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <vector>
int main(){
    std::string s;
    std::getline(std::cin, s);
    while(!s.empty() && (s.back()=='\r'||s.back()=='\n')) s.pop_back();
    std::vector<char> st;
    bool ok = true;
    for(char c : s){
        if(c=='('||c=='['||c=='{') st.push_back(c);
        else if(c==')'||c==']'||c=='}'){
            char m = c==')'?'(':(c==']'?'[':'{');
            if(st.empty() || st.back()!=m){ ok=false; break; }
            st.pop_back();
        }
    }
    std::cout << ((ok && st.empty()) ? "YES" : "NO") << "\n";
    return 0;
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
    match := map[byte]byte{')': '(', ']': '[', '}': '{'}
    st := []byte{}
    ok := true
    for i := 0; i < len(line); i++ {
        c := line[i]
        if c == '(' || c == '[' || c == '{' {
            st = append(st, c)
        } else if m, isClose := match[c]; isClose {
            if len(st) == 0 || st[len(st)-1] != m {
                ok = false
                break
            }
            st = st[:len(st)-1]
        }
    }
    if ok && len(st) == 0 {
        fmt.Println("YES")
    } else {
        fmt.Println("NO")
    }
}
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim_end_matches(|c| c == '\n' || c == '\r');
    let mut st: Vec<char> = Vec::new();
    let mut ok = true;
    for c in s.chars() {
        match c {
            '(' | '[' | '{' => st.push(c),
            ')' | ']' | '}' => {
                let m = match c { ')' => '(', ']' => '[', _ => '{' };
                if st.pop() != Some(m) { ok = false; break; }
            }
            _ => {}
        }
    }
    println!("{}", if ok && st.is_empty() { "YES" } else { "NO" });
}
""",
            "csharp": r"""using System;
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
""",
        },
    },
    # 3 -------------------------------------------------------------- #
    {
        "slug": "roman-to-int",
        "title": "Roman numeral to integer",
        "desc": ("Convert an uppercase Roman numeral to its integer value. "
                 "Use the subtractive rule: a smaller symbol immediately "
                 "before a larger one is subtracted (IV=4, IX=9, XL=40, "
                 "XC=90, CD=400, CM=900)."),
        "stdin": "one line: a valid Roman numeral (I V X L C D M)",
        "stdout": "the integer value",
        "cases": [
            ("III\n", "3"),
            ("IV\n", "4"),
            ("IX\n", "9"),
            ("LVIII\n", "58"),
            ("MCMXCIV\n", "1994"),
            ("XL\n", "40"),
            ("MMXXIV\n", "2024"),
        ],
        "refs": {
            "python": r"""import sys
s = sys.stdin.readline().strip()
val = {"I":1,"V":5,"X":10,"L":50,"C":100,"D":500,"M":1000}
total = 0
for i, c in enumerate(s):
    if i + 1 < len(s) and val[c] < val[s[i+1]]:
        total -= val[c]
    else:
        total += val[c]
print(total)
""",
            "c": r"""#include <stdio.h>
#include <string.h>
static int v(char c){
    switch(c){case 'I':return 1;case 'V':return 5;case 'X':return 10;
        case 'L':return 50;case 'C':return 100;case 'D':return 500;
        case 'M':return 1000;default:return 0;}
}
int main(void){
    char s[256];
    if(scanf("%255s", s)!=1) return 0;
    int n=(int)strlen(s);
    long total=0;
    for(int i=0;i<n;i++){
        if(i+1<n && v(s[i])<v(s[i+1])) total-=v(s[i]);
        else total+=v(s[i]);
    }
    printf("%ld\n", total);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <map>
int main(){
    std::string s;
    std::cin >> s;
    std::map<char,int> v{{'I',1},{'V',5},{'X',10},{'L',50},
        {'C',100},{'D',500},{'M',1000}};
    long total = 0;
    int n = (int)s.size();
    for(int i=0;i<n;i++){
        if(i+1<n && v[s[i]]<v[s[i+1]]) total -= v[s[i]];
        else total += v[s[i]];
    }
    std::cout << total << "\n";
    return 0;
}
""",
            "go": r"""package main

import "fmt"

func v(c byte) int {
    switch c {
    case 'I':
        return 1
    case 'V':
        return 5
    case 'X':
        return 10
    case 'L':
        return 50
    case 'C':
        return 100
    case 'D':
        return 500
    case 'M':
        return 1000
    }
    return 0
}

func main() {
    var s string
    fmt.Scan(&s)
    total := 0
    n := len(s)
    for i := 0; i < n; i++ {
        if i+1 < n && v(s[i]) < v(s[i+1]) {
            total -= v(s[i])
        } else {
            total += v(s[i])
        }
    }
    fmt.Println(total)
}
""",
            "rust": r"""use std::io::{self, Read};
fn v(c: u8) -> i64 {
    match c {
        b'I' => 1, b'V' => 5, b'X' => 10, b'L' => 50,
        b'C' => 100, b'D' => 500, b'M' => 1000, _ => 0,
    }
}
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim().as_bytes();
    let n = s.len();
    let mut total: i64 = 0;
    for i in 0..n {
        if i + 1 < n && v(s[i]) < v(s[i + 1]) {
            total -= v(s[i]);
        } else {
            total += v(s[i]);
        }
    }
    println!("{}", total);
}
""",
            "csharp": r"""using System;
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
""",
        },
    },
    # 4 -------------------------------------------------------------- #
    {
        "slug": "merge-intervals",
        "title": "Merge intervals",
        "desc": ("Merge overlapping closed integer intervals. The input is a "
                 "count N followed by 2*N integers giving N intervals as "
                 "(start, end) pairs. Merge intervals that overlap or touch "
                 "(end == next start counts as overlap) and print the merged "
                 "intervals sorted by start, as a flat space-separated list of "
                 "start end start end ..."),
        "stdin": "N then 2*N integers (whitespace-separated, any layout)",
        "stdout": "the merged intervals flattened: s1 e1 s2 e2 ...",
        "cases": [
            ("3\n1 3 2 6 8 10\n", "1 6 8 10"),
            ("1\n5 7\n", "5 7"),
            ("2\n1 4 4 5\n", "1 5"),
            ("3\n1 2 3 4 5 6\n", "1 2 3 4 5 6"),
            ("2\n1 10 2 3\n", "1 10"),
            ("4 1 3 0 0 9 12 2 4\n", "0 0 1 4 9 12"),
        ],
        "refs": {
            "python": r"""import sys
nums = list(map(int, sys.stdin.read().split()))
n = nums[0]
pts = nums[1:1 + 2 * n]
iv = sorted((pts[2*i], pts[2*i+1]) for i in range(n))
out = []
for s, e in iv:
    if out and s <= out[-1][1]:
        out[-1] = (out[-1][0], max(out[-1][1], e))
    else:
        out.append((s, e))
print(" ".join(str(x) for s, e in out for x in (s, e)))
""",
            "c": r"""#include <stdio.h>
#include <stdlib.h>
typedef struct { long s, e; } Iv;
static int cmp(const void *a, const void *b){
    const Iv *x=a,*y=b;
    if(x->s<y->s) return -1;
    if(x->s>y->s) return 1;
    return (x->e>y->e)-(x->e<y->e);
}
int main(void){
    long n;
    if(scanf("%ld", &n)!=1) return 0;
    Iv *iv = malloc(sizeof(Iv)*(n>0?n:1));
    for(long i=0;i<n;i++) if(scanf("%ld %ld", &iv[i].s, &iv[i].e)!=2) return 0;
    qsort(iv, n, sizeof(Iv), cmp);
    long os[100000], oe[100000];
    int k=0;
    for(long i=0;i<n;i++){
        if(k>0 && iv[i].s <= oe[k-1]){
            if(iv[i].e > oe[k-1]) oe[k-1]=iv[i].e;
        } else { os[k]=iv[i].s; oe[k]=iv[i].e; k++; }
    }
    for(int i=0;i<k;i++){
        printf("%ld %ld%s", os[i], oe[i], i+1<k?" ":"");
    }
    printf("\n");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <vector>
#include <algorithm>
int main(){
    long n;
    if(!(std::cin >> n)) return 0;
    std::vector<std::pair<long,long>> iv(n);
    for(long i=0;i<n;i++) std::cin >> iv[i].first >> iv[i].second;
    std::sort(iv.begin(), iv.end());
    std::vector<std::pair<long,long>> out;
    for(auto &p : iv){
        if(!out.empty() && p.first <= out.back().second)
            out.back().second = std::max(out.back().second, p.second);
        else out.push_back(p);
    }
    for(size_t i=0;i<out.size();i++){
        std::cout << out[i].first << " " << out[i].second;
        if(i+1<out.size()) std::cout << " ";
    }
    std::cout << "\n";
    return 0;
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "sort"
    "strconv"
    "strings"
)

func main() {
    r := bufio.NewReader(os.Stdin)
    var nums []int
    sc := bufio.NewScanner(r)
    sc.Buffer(make([]byte, 1024*1024), 1024*1024)
    sc.Split(bufio.ScanWords)
    for sc.Scan() {
        x, _ := strconv.Atoi(sc.Text())
        nums = append(nums, x)
    }
    if len(nums) == 0 {
        return
    }
    n := nums[0]
    type iv struct{ s, e int }
    arr := make([]iv, n)
    for i := 0; i < n; i++ {
        arr[i] = iv{nums[1+2*i], nums[2+2*i]}
    }
    sort.Slice(arr, func(a, b int) bool {
        if arr[a].s != arr[b].s {
            return arr[a].s < arr[b].s
        }
        return arr[a].e < arr[b].e
    })
    var out []iv
    for _, p := range arr {
        if len(out) > 0 && p.s <= out[len(out)-1].e {
            if p.e > out[len(out)-1].e {
                out[len(out)-1].e = p.e
            }
        } else {
            out = append(out, p)
        }
    }
    var parts []string
    for _, p := range out {
        parts = append(parts, strconv.Itoa(p.s), strconv.Itoa(p.e))
    }
    fmt.Println(strings.Join(parts, " "))
}
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let nums: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    if nums.is_empty() { return; }
    let n = nums[0] as usize;
    let mut iv: Vec<(i64, i64)> = (0..n)
        .map(|i| (nums[1 + 2 * i], nums[2 + 2 * i])).collect();
    iv.sort();
    let mut out: Vec<(i64, i64)> = Vec::new();
    for (st, en) in iv {
        if let Some(last) = out.last_mut() {
            if st <= last.1 {
                if en > last.1 { last.1 = en; }
                continue;
            }
        }
        out.push((st, en));
    }
    let parts: Vec<String> = out.iter()
        .flat_map(|&(s, e)| vec![s.to_string(), e.to_string()]).collect();
    println!("{}", parts.join(" "));
}
""",
            "csharp": r"""using System;
using System.Linq;
using System.Collections.Generic;
class Sol {
    static void Main() {
        var nums = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        if (nums.Length == 0) return;
        int n = (int)nums[0];
        var iv = new List<(long s, long e)>();
        for (int i = 0; i < n; i++) iv.Add((nums[1 + 2 * i], nums[2 + 2 * i]));
        iv.Sort((a, b) => a.s != b.s ? a.s.CompareTo(b.s) : a.e.CompareTo(b.e));
        var outp = new List<(long s, long e)>();
        foreach (var p in iv) {
            if (outp.Count > 0 && p.s <= outp[^1].e) {
                if (p.e > outp[^1].e) outp[^1] = (outp[^1].s, p.e);
            } else outp.Add(p);
        }
        Console.WriteLine(string.Join(" ",
            outp.SelectMany(p => new[]{p.s, p.e})));
    }
}
""",
        },
    },
    # 5 -------------------------------------------------------------- #
    {
        "slug": "expr-eval",
        "title": "Evaluate expression (precedence)",
        "desc": ("Evaluate an arithmetic expression of non-negative integers "
                 "with the binary operators +, -, and * (no parentheses, no "
                 "division). Multiplication binds tighter than + and -; "
                 "+ and - are left-associative. There are no spaces."),
        "stdin": "one line: an expression like 2+3*4",
        "stdout": "the integer result",
        "cases": [
            ("2+3*4\n", "14"),
            ("2*3+4\n", "10"),
            ("10-2*3\n", "4"),
            ("1+2+3+4\n", "10"),
            ("5\n", "5"),
            ("2*3*4\n", "24"),
            ("100-50-25\n", "25"),
        ],
        "refs": {
            "python": r"""import sys
s = sys.stdin.readline().strip()
# tokenize
toks = []
i = 0
while i < len(s):
    if s[i].isdigit():
        j = i
        while j < len(s) and s[j].isdigit():
            j += 1
        toks.append(int(s[i:j]))
        i = j
    else:
        toks.append(s[i])
        i += 1
# pass 1: collapse '*'
red = [toks[0]]
k = 1
while k < len(toks):
    op = toks[k]
    val = toks[k+1]
    if op == '*':
        red[-1] = red[-1] * val
    else:
        red.append(op)
        red.append(val)
    k += 2
# pass 2: left-to-right + and -
total = red[0]
k = 1
while k < len(red):
    if red[k] == '+':
        total += red[k+1]
    else:
        total -= red[k+1]
    k += 2
print(total)
""",
            "c": r"""#include <stdio.h>
#include <string.h>
#include <ctype.h>
int main(void){
    char s[100000];
    if(!fgets(s, sizeof(s), stdin)) return 0;
    long nums[50000]; char ops[50000];
    int nn=0, no=0;
    int i=0, n=(int)strlen(s);
    long cur=0; int have=0;
    while(i<n){
        char c=s[i];
        if(isdigit((unsigned char)c)){ cur=cur*10+(c-'0'); have=1; i++; }
        else if(c=='+'||c=='-'||c=='*'){
            nums[nn++]=cur; cur=0; have=0; ops[no++]=c; i++;
        } else i++;
    }
    if(have || nn==0) nums[nn++]=cur;
    /* collapse * */
    long rnums[50000]; char rops[50000];
    int rn=0, ro=0;
    rnums[rn++]=nums[0];
    for(int k=0;k<no;k++){
        if(ops[k]=='*') rnums[rn-1]=rnums[rn-1]*nums[k+1];
        else { rops[ro++]=ops[k]; rnums[rn++]=nums[k+1]; }
    }
    long total=rnums[0];
    for(int k=0;k<ro;k++){
        if(rops[k]=='+') total+=rnums[k+1]; else total-=rnums[k+1];
    }
    printf("%ld\n", total);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <vector>
#include <cctype>
int main(){
    std::string s;
    std::getline(std::cin, s);
    std::vector<long> nums; std::vector<char> ops;
    long cur=0; bool have=false;
    for(char c : s){
        if(isdigit((unsigned char)c)){ cur=cur*10+(c-'0'); have=true; }
        else if(c=='+'||c=='-'||c=='*'){ nums.push_back(cur); cur=0; have=false; ops.push_back(c); }
    }
    if(have || nums.empty()) nums.push_back(cur);
    std::vector<long> rn; std::vector<char> ro;
    rn.push_back(nums[0]);
    for(size_t k=0;k<ops.size();k++){
        if(ops[k]=='*') rn.back()=rn.back()*nums[k+1];
        else { ro.push_back(ops[k]); rn.push_back(nums[k+1]); }
    }
    long total=rn[0];
    for(size_t k=0;k<ro.size();k++){
        if(ro[k]=='+') total+=rn[k+1]; else total-=rn[k+1];
    }
    std::cout << total << "\n";
    return 0;
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
    var nums []int64
    var ops []byte
    var cur int64
    have := false
    for i := 0; i < len(line); i++ {
        c := line[i]
        if c >= '0' && c <= '9' {
            cur = cur*10 + int64(c-'0')
            have = true
        } else if c == '+' || c == '-' || c == '*' {
            nums = append(nums, cur)
            cur = 0
            have = false
            ops = append(ops, c)
        }
    }
    if have || len(nums) == 0 {
        nums = append(nums, cur)
    }
    rn := []int64{nums[0]}
    var ro []byte
    for k := 0; k < len(ops); k++ {
        if ops[k] == '*' {
            rn[len(rn)-1] = rn[len(rn)-1] * nums[k+1]
        } else {
            ro = append(ro, ops[k])
            rn = append(rn, nums[k+1])
        }
    }
    total := rn[0]
    for k := 0; k < len(ro); k++ {
        if ro[k] == '+' {
            total += rn[k+1]
        } else {
            total -= rn[k+1]
        }
    }
    fmt.Println(total)
}
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let s = s.trim();
    let mut nums: Vec<i64> = Vec::new();
    let mut ops: Vec<char> = Vec::new();
    let mut cur: i64 = 0;
    let mut have = false;
    for c in s.chars() {
        if c.is_ascii_digit() {
            cur = cur * 10 + (c as i64 - '0' as i64);
            have = true;
        } else if c == '+' || c == '-' || c == '*' {
            nums.push(cur);
            cur = 0;
            have = false;
            ops.push(c);
        }
    }
    if have || nums.is_empty() {
        nums.push(cur);
    }
    let mut rn: Vec<i64> = vec![nums[0]];
    let mut ro: Vec<char> = Vec::new();
    for k in 0..ops.len() {
        if ops[k] == '*' {
            let last = rn.len() - 1;
            rn[last] *= nums[k + 1];
        } else {
            ro.push(ops[k]);
            rn.push(nums[k + 1]);
        }
    }
    let mut total = rn[0];
    for k in 0..ro.len() {
        if ro[k] == '+' { total += rn[k + 1]; } else { total -= rn[k + 1]; }
    }
    println!("{}", total);
}
""",
            "csharp": r"""using System;
using System.Collections.Generic;
class Sol {
    static void Main() {
        string s = (Console.ReadLine() ?? "").Trim();
        var nums = new List<long>();
        var ops = new List<char>();
        long cur = 0; bool have = false;
        foreach (char c in s) {
            if (c >= '0' && c <= '9') { cur = cur * 10 + (c - '0'); have = true; }
            else if (c == '+' || c == '-' || c == '*') {
                nums.Add(cur); cur = 0; have = false; ops.Add(c);
            }
        }
        if (have || nums.Count == 0) nums.Add(cur);
        var rn = new List<long> { nums[0] };
        var ro = new List<char>();
        for (int k = 0; k < ops.Count; k++) {
            if (ops[k] == '*') rn[rn.Count - 1] = rn[rn.Count - 1] * nums[k + 1];
            else { ro.Add(ops[k]); rn.Add(nums[k + 1]); }
        }
        long total = rn[0];
        for (int k = 0; k < ro.Count; k++) {
            if (ro[k] == '+') total += rn[k + 1]; else total -= rn[k + 1];
        }
        Console.WriteLine(total);
    }
}
""",
        },
    },
    # 6 -------------------------------------------------------------- #
    {
        "slug": "spiral-order",
        "title": "Spiral matrix traversal",
        "desc": ("Read a matrix and print its elements in clockwise spiral "
                 "order starting from the top-left, going right, then down, "
                 "then left, then up, spiralling inward."),
        "stdin": "first two integers R and C, then R*C integers row-major",
        "stdout": "the R*C values in spiral order, space-separated",
        "cases": [
            ("3 3\n1 2 3 4 5 6 7 8 9\n", "1 2 3 6 9 8 7 4 5"),
            ("1 4\n1 2 3 4\n", "1 2 3 4"),
            ("4 1\n1 2 3 4\n", "1 2 3 4"),
            ("2 2\n1 2 3 4\n", "1 2 4 3"),
            ("2 3\n1 2 3 4 5 6\n", "1 2 3 6 5 4"),
            ("3 2\n1 2 3 4 5 6\n", "1 2 4 6 5 3"),
        ],
        "refs": {
            "python": r"""import sys
nums = list(map(int, sys.stdin.read().split()))
R, C = nums[0], nums[1]
g = [nums[2 + i*C: 2 + (i+1)*C] for i in range(R)]
top, bot, left, right = 0, R-1, 0, C-1
out = []
while top <= bot and left <= right:
    for j in range(left, right+1):
        out.append(g[top][j])
    top += 1
    for i in range(top, bot+1):
        out.append(g[i][right])
    right -= 1
    if top <= bot:
        for j in range(right, left-1, -1):
            out.append(g[bot][j])
        bot -= 1
    if left <= right:
        for i in range(bot, top-1, -1):
            out.append(g[i][left])
        left += 1
print(" ".join(map(str, out)))
""",
            "c": r"""#include <stdio.h>
#include <stdlib.h>
int main(void){
    int R, C;
    if(scanf("%d %d", &R, &C)!=2) return 0;
    int *g = malloc(sizeof(int)*R*C);
    for(int i=0;i<R*C;i++) if(scanf("%d", &g[i])!=1) return 0;
    int top=0, bot=R-1, left=0, right=C-1, first=1;
    while(top<=bot && left<=right){
        for(int j=left;j<=right;j++){ printf("%s%d", first?"":" ", g[top*C+j]); first=0; }
        top++;
        for(int i=top;i<=bot;i++){ printf(" %d", g[i*C+right]); }
        right--;
        if(top<=bot){
            for(int j=right;j>=left;j--){ printf(" %d", g[bot*C+j]); }
            bot--;
        }
        if(left<=right){
            for(int i=bot;i>=top;i--){ printf(" %d", g[i*C+left]); }
            left++;
        }
    }
    printf("\n");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <vector>
int main(){
    int R, C;
    if(!(std::cin >> R >> C)) return 0;
    std::vector<int> g(R*C);
    for(int i=0;i<R*C;i++) std::cin >> g[i];
    int top=0, bot=R-1, left=0, right=C-1;
    bool first=true;
    auto emit=[&](int v){ if(!first) std::cout<<" "; std::cout<<v; first=false; };
    while(top<=bot && left<=right){
        for(int j=left;j<=right;j++) emit(g[top*C+j]);
        top++;
        for(int i=top;i<=bot;i++) emit(g[i*C+right]);
        right--;
        if(top<=bot){ for(int j=right;j>=left;j--) emit(g[bot*C+j]); bot--; }
        if(left<=right){ for(int i=bot;i>=top;i--) emit(g[i*C+left]); left++; }
    }
    std::cout << "\n";
    return 0;
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "strconv"
    "strings"
)

func main() {
    sc := bufio.NewScanner(os.Stdin)
    sc.Buffer(make([]byte, 1024*1024), 1024*1024)
    sc.Split(bufio.ScanWords)
    next := func() int {
        sc.Scan()
        x, _ := strconv.Atoi(sc.Text())
        return x
    }
    R := next()
    C := next()
    g := make([]int, R*C)
    for i := range g {
        g[i] = next()
    }
    top, bot, left, right := 0, R-1, 0, C-1
    var out []string
    for top <= bot && left <= right {
        for j := left; j <= right; j++ {
            out = append(out, strconv.Itoa(g[top*C+j]))
        }
        top++
        for i := top; i <= bot; i++ {
            out = append(out, strconv.Itoa(g[i*C+right]))
        }
        right--
        if top <= bot {
            for j := right; j >= left; j-- {
                out = append(out, strconv.Itoa(g[bot*C+j]))
            }
            bot--
        }
        if left <= right {
            for i := bot; i >= top; i-- {
                out = append(out, strconv.Itoa(g[i*C+left]))
            }
            left++
        }
    }
    fmt.Println(strings.Join(out, " "))
}
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let nums: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    let r = nums[0] as i64;
    let c = nums[1] as i64;
    let g = &nums[2..];
    let at = |i: i64, j: i64| g[(i * c + j) as usize];
    let (mut top, mut bot, mut left, mut right) = (0i64, r - 1, 0i64, c - 1);
    let mut out: Vec<String> = Vec::new();
    while top <= bot && left <= right {
        for j in left..=right { out.push(at(top, j).to_string()); }
        top += 1;
        for i in top..=bot { out.push(at(i, right).to_string()); }
        right -= 1;
        if top <= bot {
            let mut j = right;
            while j >= left { out.push(at(bot, j).to_string()); j -= 1; }
            bot -= 1;
        }
        if left <= right {
            let mut i = bot;
            while i >= top { out.push(at(i, left).to_string()); i -= 1; }
            left += 1;
        }
    }
    println!("{}", out.join(" "));
}
""",
            "csharp": r"""using System;
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
""",
        },
    },
    # 7 -------------------------------------------------------------- #
    {
        "slug": "max-subarray",
        "title": "Maximum subarray sum",
        "desc": ("Print the maximum sum of any non-empty contiguous subarray "
                 "(Kadane's algorithm). The array may be entirely negative, in "
                 "which case the answer is the largest single element."),
        "stdin": "one line of whitespace-separated integers (at least one)",
        "stdout": "the maximum contiguous subarray sum",
        "cases": [
            ("-2 1 -3 4 -1 2 1 -5 4\n", "6"),
            ("1 2 3 4\n", "10"),
            ("-1 -2 -3\n", "-1"),
            ("5\n", "5"),
            ("-5\n", "-5"),
            ("3 -2 5 -1\n", "6"),
            ("-2 -1\n", "-1"),
        ],
        "refs": {
            "python": r"""import sys
a = list(map(int, sys.stdin.read().split()))
best = cur = a[0]
for x in a[1:]:
    cur = max(x, cur + x)
    best = max(best, cur)
print(best)
""",
            "c": r"""#include <stdio.h>
int main(void){
    long x, best, cur;
    if(scanf("%ld", &x)!=1) return 0;
    best = cur = x;
    while(scanf("%ld", &x)==1){
        cur = (x > cur + x) ? x : cur + x;
        if(cur > best) best = cur;
    }
    printf("%ld\n", best);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
int main(){
    long x, best, cur;
    if(!(std::cin >> x)) return 0;
    best = cur = x;
    while(std::cin >> x){
        cur = (x > cur + x) ? x : cur + x;
        if(cur > best) best = cur;
    }
    std::cout << best << "\n";
    return 0;
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "strconv"
)

func main() {
    sc := bufio.NewScanner(os.Stdin)
    sc.Buffer(make([]byte, 1024*1024), 1024*1024)
    sc.Split(bufio.ScanWords)
    first := true
    var best, cur int64
    for sc.Scan() {
        x, _ := strconv.ParseInt(sc.Text(), 10, 64)
        if first {
            best, cur = x, x
            first = false
        } else {
            if x > cur+x {
                cur = x
            } else {
                cur = cur + x
            }
            if cur > best {
                best = cur
            }
        }
    }
    fmt.Println(best)
}
""",
            "rust": r"""use std::io::{self, Read};
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let a: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    let mut best = a[0];
    let mut cur = a[0];
    for &x in &a[1..] {
        cur = if x > cur + x { x } else { cur + x };
        if cur > best { best = cur; }
    }
    println!("{}", best);
}
""",
            "csharp": r"""using System;
using System.Linq;
class Sol {
    static void Main() {
        var a = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        long best = a[0], cur = a[0];
        for (int i = 1; i < a.Length; i++) {
            cur = a[i] > cur + a[i] ? a[i] : cur + a[i];
            if (cur > best) best = cur;
        }
        Console.WriteLine(best);
    }
}
""",
        },
    },
    # 8 -------------------------------------------------------------- #
    {
        "slug": "top-word",
        "title": "Most frequent word",
        "desc": ("Read whitespace-separated words and print the word that "
                 "occurs most often. If several words tie for the highest "
                 "count, print the lexicographically smallest of them."),
        "stdin": "words separated by whitespace / newlines (at least one)",
        "stdout": "the most frequent word (lexicographic tie-break)",
        "cases": [
            ("a b a c a\n", "a"),
            ("the the dog the dog\n", "the"),
            ("x y\n", "x"),
            ("banana apple banana apple\n", "apple"),
            ("one\n", "one"),
            ("b a b a c\n", "a"),
            ("zoo ant ant zoo\n", "ant"),
        ],
        "refs": {
            "python": r"""import sys
from collections import Counter
words = sys.stdin.read().split()
c = Counter(words)
best = min(c, key=lambda w: (-c[w], w))
print(best)
""",
            "c": r"""#include <stdio.h>
#include <string.h>
#include <stdlib.h>
int main(void){
    char buf[256];
    char (*w)[256] = malloc(sizeof(char[256])*100000);
    long *cnt = calloc(100000, sizeof(long));
    int m=0;
    while(scanf("%255s", buf)==1){
        int f=-1;
        for(int i=0;i<m;i++) if(strcmp(w[i],buf)==0){ f=i; break; }
        if(f<0){ strcpy(w[m], buf); cnt[m]=1; m++; }
        else cnt[f]++;
    }
    int best=-1;
    for(int i=0;i<m;i++){
        if(best<0 || cnt[i]>cnt[best] || (cnt[i]==cnt[best] && strcmp(w[i],w[best])<0))
            best=i;
    }
    if(best>=0) printf("%s\n", w[best]);
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <unordered_map>
int main(){
    std::unordered_map<std::string,long> c;
    std::string w;
    while(std::cin >> w) c[w]++;
    std::string best; long bc=-1;
    for(auto &kv : c){
        if(kv.second>bc || (kv.second==bc && kv.first<best)){
            bc=kv.second; best=kv.first;
        }
    }
    std::cout << best << "\n";
    return 0;
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
)

func main() {
    sc := bufio.NewScanner(os.Stdin)
    sc.Buffer(make([]byte, 1024*1024), 1024*1024)
    sc.Split(bufio.ScanWords)
    c := map[string]int{}
    for sc.Scan() {
        c[sc.Text()]++
    }
    best := ""
    bc := -1
    for w, n := range c {
        if n > bc || (n == bc && w < best) {
            bc = n
            best = w
        }
    }
    fmt.Println(best)
}
""",
            "rust": r"""use std::io::{self, Read};
use std::collections::HashMap;
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let mut c: HashMap<&str, i64> = HashMap::new();
    for w in s.split_whitespace() {
        *c.entry(w).or_insert(0) += 1;
    }
    let mut best = "";
    let mut bc = -1i64;
    for (w, n) in &c {
        if *n > bc || (*n == bc && *w < best) {
            bc = *n;
            best = w;
        }
    }
    println!("{}", best);
}
""",
            "csharp": r"""using System;
using System.Linq;
using System.Collections.Generic;
class Sol {
    static void Main() {
        var words = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        var c = new Dictionary<string,long>();
        foreach (var w in words) {
            c.TryGetValue(w, out long v);
            c[w] = v + 1;
        }
        string best = null; long bc = -1;
        foreach (var kv in c) {
            if (kv.Value > bc || (kv.Value == bc && string.CompareOrdinal(kv.Key, best) < 0)) {
                bc = kv.Value; best = kv.Key;
            }
        }
        Console.WriteLine(best);
    }
}
""",
        },
    },
    # 9 -------------------------------------------------------------- #
    {
        "slug": "base-convert",
        "title": "Base conversion",
        "desc": ("Convert a non-negative integer from one base to another. "
                 "Bases are between 2 and 16; digits a-f are lowercase and "
                 "represent 10-15. Output uses lowercase digits and has no "
                 "leading zeros (except the value zero, printed as 0)."),
        "stdin": "one line: from_base to_base value (value in from_base)",
        "stdout": "the value written in to_base",
        "cases": [
            ("16 2 ff\n", "11111111"),
            ("2 10 1010\n", "10"),
            ("10 16 255\n", "ff"),
            ("10 2 0\n", "0"),
            ("8 10 17\n", "15"),
            ("10 16 4096\n", "1000"),
            ("16 10 1a\n", "26"),
        ],
        "refs": {
            "python": r"""import sys
parts = sys.stdin.read().split()
fb, tb, val = int(parts[0]), int(parts[1]), parts[2]
n = int(val, fb)
if n == 0:
    print("0")
else:
    digs = "0123456789abcdef"
    out = []
    while n > 0:
        out.append(digs[n % tb])
        n //= tb
    print("".join(reversed(out)))
""",
            "c": r"""#include <stdio.h>
#include <string.h>
static int dv(char c){
    if(c>='0'&&c<='9') return c-'0';
    return c-'a'+10;
}
int main(void){
    int fb, tb; char val[256];
    if(scanf("%d %d %255s", &fb, &tb, val)!=3) return 0;
    long long n=0;
    for(int i=0;val[i];i++) n = n*fb + dv(val[i]);
    if(n==0){ printf("0\n"); return 0; }
    const char *digs="0123456789abcdef";
    char out[128]; int k=0;
    while(n>0){ out[k++]=digs[n%tb]; n/=tb; }
    for(int i=k-1;i>=0;i--) putchar(out[i]);
    putchar('\n');
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <string>
#include <algorithm>
static int dv(char c){ return (c>='0'&&c<='9') ? c-'0' : c-'a'+10; }
int main(){
    int fb, tb; std::string val;
    std::cin >> fb >> tb >> val;
    long long n=0;
    for(char c : val) n = n*fb + dv(c);
    if(n==0){ std::cout << "0\n"; return 0; }
    const std::string digs="0123456789abcdef";
    std::string out;
    while(n>0){ out += digs[n%tb]; n/=tb; }
    std::reverse(out.begin(), out.end());
    std::cout << out << "\n";
    return 0;
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
)

func dv(c byte) int64 {
    if c >= '0' && c <= '9' {
        return int64(c - '0')
    }
    return int64(c-'a') + 10
}

func main() {
    r := bufio.NewReader(os.Stdin)
    var fb, tb int64
    var val string
    fmt.Fscan(r, &fb, &tb, &val)
    var n int64
    for i := 0; i < len(val); i++ {
        n = n*fb + dv(val[i])
    }
    if n == 0 {
        fmt.Println("0")
        return
    }
    digs := "0123456789abcdef"
    out := []byte{}
    for n > 0 {
        out = append(out, digs[n%tb])
        n /= tb
    }
    for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
        out[i], out[j] = out[j], out[i]
    }
    fmt.Println(string(out))
}
""",
            "rust": r"""use std::io::{self, Read};
fn dv(c: u8) -> i64 {
    if c.is_ascii_digit() { (c - b'0') as i64 } else { (c - b'a') as i64 + 10 }
}
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let parts: Vec<&str> = s.split_whitespace().collect();
    let fb: i64 = parts[0].parse().unwrap();
    let tb: i64 = parts[1].parse().unwrap();
    let mut n: i64 = 0;
    for &c in parts[2].as_bytes() {
        n = n * fb + dv(c);
    }
    if n == 0 {
        println!("0");
        return;
    }
    let digs = b"0123456789abcdef";
    let mut out: Vec<u8> = Vec::new();
    while n > 0 {
        out.push(digs[(n % tb) as usize]);
        n /= tb;
    }
    out.reverse();
    println!("{}", String::from_utf8(out).unwrap());
}
""",
            "csharp": r"""using System;
class Sol {
    static long Dv(char c) {
        return (c >= '0' && c <= '9') ? c - '0' : c - 'a' + 10;
    }
    static void Main() {
        var parts = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries);
        long fb = long.Parse(parts[0]), tb = long.Parse(parts[1]);
        long n = 0;
        foreach (char c in parts[2]) n = n * fb + Dv(c);
        if (n == 0) { Console.WriteLine("0"); return; }
        string digs = "0123456789abcdef";
        var chars = new System.Collections.Generic.List<char>();
        while (n > 0) { chars.Add(digs[(int)(n % tb)]); n /= tb; }
        chars.Reverse();
        Console.WriteLine(new string(chars.ToArray()));
    }
}
""",
        },
    },
    # 10 ------------------------------------------------------------- #
    {
        "slug": "window-max",
        "title": "Sliding window maximum",
        "desc": ("Given a window size k and a list of integers, print the "
                 "maximum of each contiguous window of size k as the window "
                 "slides left to right. There are (len - k + 1) windows."),
        "stdin": "first integer k, then the list integers (len >= k >= 1)",
        "stdout": "the per-window maxima, space-separated",
        "cases": [
            ("3\n1 3 -1 -3 5 3 6 7\n", "3 3 5 5 6 7"),
            ("1\n4 2 9\n", "4 2 9"),
            ("2\n1 2 3 4\n", "2 3 4"),
            ("4\n4 3 2 1\n", "4"),
            ("2\n5 5 5\n", "5 5"),
            ("3\n9 1 1 1 9\n", "9 1 9"),
        ],
        "refs": {
            "python": r"""import sys
from collections import deque
nums = list(map(int, sys.stdin.read().split()))
k = nums[0]
a = nums[1:]
dq = deque()
out = []
for i, x in enumerate(a):
    while dq and a[dq[-1]] <= x:
        dq.pop()
    dq.append(i)
    if dq[0] <= i - k:
        dq.popleft()
    if i >= k - 1:
        out.append(a[dq[0]])
print(" ".join(map(str, out)))
""",
            "c": r"""#include <stdio.h>
#include <stdlib.h>
int main(void){
    long k;
    if(scanf("%ld", &k)!=1) return 0;
    long cap=1024, m=0;
    long *a=malloc(sizeof(long)*cap);
    long x;
    while(scanf("%ld", &x)==1){
        if(m==cap){ cap*=2; a=realloc(a, sizeof(long)*cap); }
        a[m++]=x;
    }
    long *dq=malloc(sizeof(long)*(m>0?m:1));
    int head=0, tail=0, first=1;
    for(long i=0;i<m;i++){
        while(tail>head && a[dq[tail-1]] <= a[i]) tail--;
        dq[tail++]=i;
        if(dq[head] <= i-k) head++;
        if(i >= k-1){ printf("%s%ld", first?"":" ", a[dq[head]]); first=0; }
    }
    printf("\n");
    return 0;
}
""",
            "cpp": r"""#include <iostream>
#include <vector>
#include <deque>
int main(){
    long k;
    if(!(std::cin >> k)) return 0;
    std::vector<long> a; long x;
    while(std::cin >> x) a.push_back(x);
    std::deque<long> dq;
    bool first=true;
    for(long i=0;i<(long)a.size();i++){
        while(!dq.empty() && a[dq.back()] <= a[i]) dq.pop_back();
        dq.push_back(i);
        if(dq.front() <= i-k) dq.pop_front();
        if(i >= k-1){ if(!first) std::cout<<" "; std::cout<<a[dq.front()]; first=false; }
    }
    std::cout << "\n";
    return 0;
}
""",
            "go": r"""package main

import (
    "bufio"
    "fmt"
    "os"
    "strconv"
    "strings"
)

func main() {
    sc := bufio.NewScanner(os.Stdin)
    sc.Buffer(make([]byte, 1024*1024), 1024*1024)
    sc.Split(bufio.ScanWords)
    next := func() (int64, bool) {
        if !sc.Scan() {
            return 0, false
        }
        x, _ := strconv.ParseInt(sc.Text(), 10, 64)
        return x, true
    }
    k, _ := next()
    var a []int64
    for {
        x, ok := next()
        if !ok {
            break
        }
        a = append(a, x)
    }
    dq := []int{}
    var out []string
    for i := 0; i < len(a); i++ {
        for len(dq) > 0 && a[dq[len(dq)-1]] <= a[i] {
            dq = dq[:len(dq)-1]
        }
        dq = append(dq, i)
        if int64(dq[0]) <= int64(i)-k {
            dq = dq[1:]
        }
        if int64(i) >= k-1 {
            out = append(out, strconv.FormatInt(a[dq[0]], 10))
        }
    }
    fmt.Println(strings.Join(out, " "))
}
""",
            "rust": r"""use std::io::{self, Read};
use std::collections::VecDeque;
fn main() {
    let mut s = String::new();
    io::stdin().read_to_string(&mut s).unwrap();
    let nums: Vec<i64> = s.split_whitespace()
        .map(|x| x.parse().unwrap()).collect();
    let k = nums[0];
    let a = &nums[1..];
    let mut dq: VecDeque<usize> = VecDeque::new();
    let mut out: Vec<String> = Vec::new();
    for i in 0..a.len() {
        while let Some(&b) = dq.back() {
            if a[b] <= a[i] { dq.pop_back(); } else { break; }
        }
        dq.push_back(i);
        if (*dq.front().unwrap() as i64) <= i as i64 - k {
            dq.pop_front();
        }
        if i as i64 >= k - 1 {
            out.push(a[*dq.front().unwrap()].to_string());
        }
    }
    println!("{}", out.join(" "));
}
""",
            "csharp": r"""using System;
using System.Linq;
using System.Collections.Generic;
class Sol {
    static void Main() {
        var nums = Console.In.ReadToEnd()
            .Split(new[]{' ','\n','\r','\t'}, StringSplitOptions.RemoveEmptyEntries)
            .Select(long.Parse).ToArray();
        long k = nums[0];
        var a = nums.Skip(1).ToArray();
        var dq = new LinkedList<int>();
        var outp = new List<long>();
        for (int i = 0; i < a.Length; i++) {
            while (dq.Count > 0 && a[dq.Last.Value] <= a[i]) dq.RemoveLast();
            dq.AddLast(i);
            if (dq.First.Value <= i - k) dq.RemoveFirst();
            if (i >= k - 1) outp.Add(a[dq.First.Value]);
        }
        Console.WriteLine(string.Join(" ", outp));
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
"""Oracle for @@ID@@ — AUTO-GENERATED by gen_coder_med.py; do not hand-edit."""

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
        "tier: medium\n"
        f"source: coder_med/{problem['slug']}\n"
        f"sandbox_path: solution{ext}\n"
        f"oracle: {qid}-oracle.py\n"
        f"language: {lang}\n"
        "task: |\n"
        f"{_task_block(problem, lang, ext)}\n"
        "notes: |\n"
        "  Auto-generated stdin/stdout easy-medium problem (suite coder_med).\n"
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
"""pytest config for the coder_med oracle suite (AUTO-GENERATED).

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
    suite_hash = _hash_for_suite(SUITE_DIR, ids)
    return (
        "---\n"
        "suite: coder_med\n"
        f'suite_version: "{SUITE_VERSION}"\n'
        "released: 2026-06-04\n"
        f"suite_hash: {suite_hash}\n"
        "manifest:\n"
        f"{manifest}\n"
        "rubric:\n"
        "  pass_rate_floor: 0.70\n"
        "  quality_score_floor: 3.5\n"
        "  tie_breaker: median_tokens\n"
        "---\n\n"
        "# Coder Medium Skill-Eval Suite v1.0\n\n"
        "Multi-language **easy-medium** coder suite — the discrimination band "
        "between `coder_easy` (saturates at ~100% for the strong models) and "
        "`coder_mlang` (defeats every model). 10 problems × 6 languages = "
        "**60 questions**, one uniform contract: read stdin, write stdout. The "
        "problems need real algorithmic thinking + edge-case handling so "
        "careless implementations fail the parametrized cases.\n\n"
        "AUTO-GENERATED by `benchmarks/consultants/gen_coder_med.py` — edit the "
        "spec there and regenerate; do not hand-edit question files.\n\n"
        "## Rubric\n\n"
        "A model **qualifies** iff `pass_rate ≥ 0.70` AND `avg_quality ≥ 3.5`. "
        "Among qualifying models the recommended default is the highest "
        "pass-rate; ties break on `median_tokens`. Per-language winners are "
        "derived the same way over each language's 10 questions.\n\n"
        "## Languages\n\n"
        "Python, Rust, Go, C, C++, C# — toolchains pinned in run metadata via "
        "`oracles_mlang.probe_toolchain_versions()`.\n"
    )


def main() -> None:
    SUITE_DIR.mkdir(parents=True, exist_ok=True)
    # Clean previously-generated artifacts so removed problems don't linger.
    for old in SUITE_DIR.glob("*-med-*"):
        old.unlink()
    ids: list[str] = []
    n_files = 0
    for idx, problem in enumerate(PROBLEMS, start=1):
        nn = f"{idx:02d}"
        for lang, ext in LANGS.items():
            qid = f"{lang}-med-{nn}-{problem['slug']}"
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
