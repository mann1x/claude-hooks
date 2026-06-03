package main

import (
    "bufio"
    "fmt"
    "os"
    "strings"
)

func main() {
    r := bufio.NewReader(os.Stdin)
    line, _ := r.ReadString('\n')
    line = strings.TrimRight(line, "\r\n")
    fmt.Println(strings.ToUpper(line))
}
