package main

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
