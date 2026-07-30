package main

import "fmt"

func main() {
    var x int
    seen := map[int]bool{}
    var out []int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if !seen[x] {
            seen[x] = true
            out = append(out, x)
        }
    }
    for i, v := range out {
        if i > 0 {
            fmt.Print(" ")
        }
        fmt.Print(v)
    }
    fmt.Println()
}
