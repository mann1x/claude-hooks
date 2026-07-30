package main

import "fmt"

func main() {
    var x, sum int
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        if x%2 == 0 {
            sum += x
        }
    }
    fmt.Println(sum)
}
