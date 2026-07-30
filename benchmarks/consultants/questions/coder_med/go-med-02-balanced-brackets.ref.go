package main

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
