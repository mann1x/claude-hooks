package main

import "fmt"

func main() {
    var x, acc int
    first := true
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        acc += x
        if !first {
            fmt.Print(" ")
        }
        first = false
        fmt.Print(acc)
    }
    fmt.Println()
}
