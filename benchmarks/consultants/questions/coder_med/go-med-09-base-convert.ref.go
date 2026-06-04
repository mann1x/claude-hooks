package main

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
