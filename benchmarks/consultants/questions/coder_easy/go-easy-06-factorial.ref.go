package main

import "fmt"

func main() {
    var n int
    fmt.Scan(&n)
    r := int64(1)
    for i := int64(2); i <= int64(n); i++ {
        r *= i
    }
    fmt.Println(r)
}
