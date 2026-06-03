package main

import "fmt"

func main() {
    var a, b int64
    fmt.Scan(&a, &b)
    r := int64(1)
    for i := int64(0); i < b; i++ {
        r *= a
    }
    fmt.Println(r)
}
