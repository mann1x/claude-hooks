package main

import "fmt"

func main() {
    var x int
    sum, count := 0, 0
    for {
        if _, err := fmt.Scan(&x); err != nil {
            break
        }
        sum += x
        count++
    }
    fmt.Println(sum / count)
}
