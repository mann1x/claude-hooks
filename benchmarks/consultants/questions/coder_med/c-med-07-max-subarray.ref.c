#include <stdio.h>
int main(void){
    long x, best, cur;
    if(scanf("%ld", &x)!=1) return 0;
    best = cur = x;
    while(scanf("%ld", &x)==1){
        cur = (x > cur + x) ? x : cur + x;
        if(cur > best) best = cur;
    }
    printf("%ld\n", best);
    return 0;
}
