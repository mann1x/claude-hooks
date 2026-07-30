package main

import (
    "fmt"
    "sort"
)

func main() {
    var x int
    seen := map[int]bool{}
    var v []int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if !seen[x] {
            seen[x] = true
            v = append(v, x)
        }
    }
    sort.Ints(v)
    fmt.Println(v[len(v)-2])
}
