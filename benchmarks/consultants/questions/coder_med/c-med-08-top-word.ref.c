#include <stdio.h>
#include <string.h>
#include <stdlib.h>
int main(void){
    char buf[256];
    char (*w)[256] = malloc(sizeof(char[256])*100000);
    long *cnt = calloc(100000, sizeof(long));
    int m=0;
    while(scanf("%255s", buf)==1){
        int f=-1;
        for(int i=0;i<m;i++) if(strcmp(w[i],buf)==0){ f=i; break; }
        if(f<0){ strcpy(w[m], buf); cnt[m]=1; m++; }
        else cnt[f]++;
    }
    int best=-1;
    for(int i=0;i<m;i++){
        if(best<0 || cnt[i]>cnt[best] || (cnt[i]==cnt[best] && strcmp(w[i],w[best])<0))
            best=i;
    }
    if(best>=0) printf("%s\n", w[best]);
    return 0;
}
