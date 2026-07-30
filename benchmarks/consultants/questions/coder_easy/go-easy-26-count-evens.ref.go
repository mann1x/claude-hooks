package main

import "fmt"

func main() {
    var x, n int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if x%2 == 0 {
            n++
        }
    }
    fmt.Println(n)
}
