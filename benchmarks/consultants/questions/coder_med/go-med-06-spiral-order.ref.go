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
