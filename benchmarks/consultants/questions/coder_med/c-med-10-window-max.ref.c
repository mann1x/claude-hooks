#include <stdio.h>
#include <stdlib.h>
int main(void){
    long k;
    if(scanf("%ld", &k)!=1) return 0;
    long cap=1024, m=0;
    long *a=malloc(sizeof(long)*cap);
    long x;
    while(scanf("%ld", &x)==1){
        if(m==cap){ cap*=2; a=realloc(a, sizeof(long)*cap); }
        a[m++]=x;
    }
    long *dq=malloc(sizeof(long)*(m>0?m:1));
    int head=0, tail=0, first=1;
    for(long i=0;i<m;i++){
        while(tail>head && a[dq[tail-1]] <= a[i]) tail--;
        dq[tail++]=i;
        if(dq[head] <= i-k) head++;
        if(i >= k-1){ printf("%s%ld", first?"":" ", a[dq[head]]); first=0; }
    }
    printf("\n");
    return 0;
}
