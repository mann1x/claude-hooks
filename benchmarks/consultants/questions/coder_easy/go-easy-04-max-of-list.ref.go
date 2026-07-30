package main

import "fmt"

func main() {
    var x, m int
    if _, err := fmt.Scan(&m); err != nil {
        return
    }
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if x > m {
            m = x
        }
    }
    fmt.Println(m)
}
