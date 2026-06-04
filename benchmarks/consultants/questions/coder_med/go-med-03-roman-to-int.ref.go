package main

import "fmt"

func v(c byte) int {
    switch c {
    case 'I':
        return 1
    case 'V':
        return 5
    case 'X':
        return 10
    case 'L':
        return 50
    case 'C':
        return 100
    case 'D':
        return 500
    case 'M':
        return 1000
    }
    return 0
}

func main() {
    var s string
    fmt.Scan(&s)
    total := 0
    n := len(s)
    for i := 0; i < n; i++ {
        if i+1 < n && v(s[i]) < v(s[i+1]) {
            total -= v(s[i])
        } else {
            total += v(s[i])
        }
    }
    fmt.Println(total)
}
