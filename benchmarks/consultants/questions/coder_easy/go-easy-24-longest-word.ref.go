package main

import "fmt"

func main() {
    var w, best string
    for {
        if _, err := fmt.Scan(&w); err != nil {
            break
        }
        if len(w) > len(best) {
            best = w
        }
    }
    fmt.Println(best)
}
