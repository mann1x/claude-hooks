#include <stdio.h>
#include <string.h>
int main(void){
    static char s[1000000];
    if(!fgets(s, sizeof(s), stdin)) return 0;
    int n = (int)strlen(s);
    while(n>0 && (s[n-1]=='\n'||s[n-1]=='\r')) s[--n]=0;
    int i=0;
    while(i<n){
        int j=i;
        while(j<n && s[j]==s[i]) j++;
        printf("%c%d", s[i], j-i);
        i=j;
    }
    printf("\n");
    return 0;
}
