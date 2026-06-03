package main

import (
    "fmt"
    "strings"
)

func main() {
    var w string
    first := true
    for {
        if _, err := fmt.Scan(&w); err != nil {
            break
        }
        if !first {
            fmt.Print(" ")
        }
        first = false
        fmt.Print(strings.ToUpper(w[:1]) + strings.ToLower(w[1:]))
    }
    fmt.Println()
}
