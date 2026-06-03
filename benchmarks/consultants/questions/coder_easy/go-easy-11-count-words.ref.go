package main

import (
    "fmt"
    "io"
    "os"
    "strings"
)

func main() {
    data, _ := io.ReadAll(os.Stdin)
    fmt.Println(len(strings.Fields(string(data))))
}
