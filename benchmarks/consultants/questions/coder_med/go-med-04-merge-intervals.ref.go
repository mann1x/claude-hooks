package main

import (
    "bufio"
    "fmt"
    "os"
    "sort"
    "strconv"
    "strings"
)

func main() {
    r := bufio.NewReader(os.Stdin)
    var nums []int
    sc := bufio.NewScanner(r)
    sc.Buffer(make([]byte, 1024*1024), 1024*1024)
    sc.Split(bufio.ScanWords)
    for sc.Scan() {
        x, _ := strconv.Atoi(sc.Text())
        nums = append(nums, x)
    }
    if len(nums) == 0 {
        return
    }
    n := nums[0]
    type iv struct{ s, e int }
    arr := make([]iv, n)
    for i := 0; i < n; i++ {
        arr[i] = iv{nums[1+2*i], nums[2+2*i]}
    }
    sort.Slice(arr, func(a, b int) bool {
        if arr[a].s != arr[b].s {
            return arr[a].s < arr[b].s
        }
        return arr[a].e < arr[b].e
    })
    var out []iv
    for _, p := range arr {
        if len(out) > 0 && p.s <= out[len(out)-1].e {
            if p.e > out[len(out)-1].e {
                out[len(out)-1].e = p.e
            }
        } else {
            out = append(out, p)
        }
    }
    var parts []string
    for _, p := range out {
        parts = append(parts, strconv.Itoa(p.s), strconv.Itoa(p.e))
    }
    fmt.Println(strings.Join(parts, " "))
}
