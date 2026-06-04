package main

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
