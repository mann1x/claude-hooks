package main

import (
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

func main() {
	data, _ := io.ReadAll(os.Stdin)
	sum := 0
	for _, f := range strings.Fields(string(data)) {
		n, _ := strconv.Atoi(f)
		sum += n
	}
	fmt.Println(sum)
}
