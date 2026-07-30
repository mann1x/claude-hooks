package main

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
