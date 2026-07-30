package main

import "fmt"

func main() {
    var n int
    fmt.Scan(&n)
    p := n >= 2
    for i := 2; i*i <= n; i++ {
        if n%i == 0 {
            p = false
            break
        }
    }
    if p {
        fmt.Println("yes")
    } else {
        fmt.Println("no")
    }
}
