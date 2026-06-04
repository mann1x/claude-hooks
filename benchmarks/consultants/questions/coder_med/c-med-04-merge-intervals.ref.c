#include <stdio.h>
#include <stdlib.h>
typedef struct { long s, e; } Iv;
static int cmp(const void *a, const void *b){
    const Iv *x=a,*y=b;
    if(x->s<y->s) return -1;
    if(x->s>y->s) return 1;
    return (x->e>y->e)-(x->e<y->e);
}
int main(void){
    long n;
    if(scanf("%ld", &n)!=1) return 0;
    Iv *iv = malloc(sizeof(Iv)*(n>0?n:1));
    for(long i=0;i<n;i++) if(scanf("%ld %ld", &iv[i].s, &iv[i].e)!=2) return 0;
    qsort(iv, n, sizeof(Iv), cmp);
    long os[100000], oe[100000];
    int k=0;
    for(long i=0;i<n;i++){
        if(k>0 && iv[i].s <= oe[k-1]){
            if(iv[i].e > oe[k-1]) oe[k-1]=iv[i].e;
        } else { os[k]=iv[i].s; oe[k]=iv[i].e; k++; }
    }
    for(int i=0;i<k;i++){
        printf("%ld %ld%s", os[i], oe[i], i+1<k?" ":"");
    }
    printf("\n");
    return 0;
}
