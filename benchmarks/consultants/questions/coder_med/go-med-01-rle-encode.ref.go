package main

import (
    "bufio"
    "fmt"
    "os"
    "strconv"
    "strings"
)

func main() {
    r := bufio.NewReader(os.Stdin)
    line, _ := r.ReadString('\n')
    line = strings.TrimRight(line, "\r\n")
    var b strings.Builder
    n := len(line)
    i := 0
    for i < n {
        j := i
        for j < n && line[j] == line[i] {
            j++
        }
        b.WriteByte(line[i])
        b.WriteString(strconv.Itoa(j - i))
        i = j
    }
    fmt.Println(b.String())
}
