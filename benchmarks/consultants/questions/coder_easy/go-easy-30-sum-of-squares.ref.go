package main

import "fmt"

func main() {
    var x, sum int64
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        sum += x * x
    }
    fmt.Println(sum)
}
