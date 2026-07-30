package main

import "fmt"

func main() {
    var tok string
    fmt.Scan(&tok)
    sum := 0
    for _, c := range tok {
        sum += int(c - '0')
    }
    fmt.Println(sum)
}
