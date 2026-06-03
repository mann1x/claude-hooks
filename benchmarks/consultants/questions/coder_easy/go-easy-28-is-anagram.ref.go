package main

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
