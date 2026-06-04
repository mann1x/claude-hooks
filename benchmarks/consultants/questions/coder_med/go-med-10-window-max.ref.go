package main

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
