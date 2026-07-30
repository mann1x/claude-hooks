package main

import (
	"fmt"
	"io"
	"os"
	"strings"
)

func main() {
	data, _ := io.ReadAll(os.Stdin)
	s := strings.ToLower(string(data))
	n := 0
	for _, ch := range s {
		if strings.ContainsRune("aeiou", ch) {
			n++
		}
	}
	fmt.Println(n)
}
