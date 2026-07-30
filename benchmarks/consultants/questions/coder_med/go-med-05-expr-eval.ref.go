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
    var nums []int64
    var ops []byte
    var cur int64
    have := false
    for i := 0; i < len(line); i++ {
        c := line[i]
        if c >= '0' && c <= '9' {
            cur = cur*10 + int64(c-'0')
            have = true
        } else if c == '+' || c == '-' || c == '*' {
            nums = append(nums, cur)
            cur = 0
            have = false
            ops = append(ops, c)
        }
    }
    if have || len(nums) == 0 {
        nums = append(nums, cur)
    }
    rn := []int64{nums[0]}
    var ro []byte
    for k := 0; k < len(ops); k++ {
        if ops[k] == '*' {
            rn[len(rn)-1] = rn[len(rn)-1] * nums[k+1]
        } else {
            ro = append(ro, ops[k])
            rn = append(rn, nums[k+1])
        }
    }
    total := rn[0]
    for k := 0; k < len(ro); k++ {
        if ro[k] == '+' {
            total += rn[k+1]
        } else {
            total -= rn[k+1]
        }
    }
    fmt.Println(total)
}
