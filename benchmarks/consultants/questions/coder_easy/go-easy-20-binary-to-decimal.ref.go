package main

import (
    "fmt"
    "strconv"
)

func main() {
    var tok string
    fmt.Scan(&tok)
    v, _ := strconv.ParseInt(tok, 2, 64)
    fmt.Println(v)
}
