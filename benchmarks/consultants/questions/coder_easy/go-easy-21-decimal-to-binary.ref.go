package main

import (
    "fmt"
    "strconv"
)

func main() {
    var n int64
    fmt.Scan(&n)
    fmt.Println(strconv.FormatInt(n, 2))
}
