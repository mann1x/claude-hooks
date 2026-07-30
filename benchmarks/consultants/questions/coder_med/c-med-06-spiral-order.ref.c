#include <stdio.h>
#include <stdlib.h>
int main(void){
    int R, C;
    if(scanf("%d %d", &R, &C)!=2) return 0;
    int *g = malloc(sizeof(int)*R*C);
    for(int i=0;i<R*C;i++) if(scanf("%d", &g[i])!=1) return 0;
    int top=0, bot=R-1, left=0, right=C-1, first=1;
    while(top<=bot && left<=right){
        for(int j=left;j<=right;j++){ printf("%s%d", first?"":" ", g[top*C+j]); first=0; }
        top++;
        for(int i=top;i<=bot;i++){ printf(" %d", g[i*C+right]); }
        right--;
        if(top<=bot){
            for(int j=right;j>=left;j--){ printf(" %d", g[bot*C+j]); }
            bot--;
        }
        if(left<=right){
            for(int i=bot;i>=top;i--){ printf(" %d", g[i*C+left]); }
            left++;
        }
    }
    printf("\n");
    return 0;
}
