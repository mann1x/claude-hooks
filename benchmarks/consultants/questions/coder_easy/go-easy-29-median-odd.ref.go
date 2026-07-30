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
    fmt.Println(v[len(v)/2])
}
