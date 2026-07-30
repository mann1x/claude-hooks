#include <stdio.h>
#include <string.h>
static int dv(char c){
    if(c>='0'&&c<='9') return c-'0';
    return c-'a'+10;
}
int main(void){
    int fb, tb; char val[256];
    if(scanf("%d %d %255s", &fb, &tb, val)!=3) return 0;
    long long n=0;
    for(int i=0;val[i];i++) n = n*fb + dv(val[i]);
    if(n==0){ printf("0\n"); return 0; }
    const char *digs="0123456789abcdef";
    char out[128]; int k=0;
    while(n>0){ out[k++]=digs[n%tb]; n/=tb; }
    for(int i=k-1;i>=0;i--) putchar(out[i]);
    putchar('\n');
    return 0;
}
