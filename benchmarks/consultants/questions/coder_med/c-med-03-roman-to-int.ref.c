#include <stdio.h>
#include <string.h>
static int v(char c){
    switch(c){case 'I':return 1;case 'V':return 5;case 'X':return 10;
        case 'L':return 50;case 'C':return 100;case 'D':return 500;
        case 'M':return 1000;default:return 0;}
}
int main(void){
    char s[256];
    if(scanf("%255s", s)!=1) return 0;
    int n=(int)strlen(s);
    long total=0;
    for(int i=0;i<n;i++){
        if(i+1<n && v(s[i])<v(s[i+1])) total-=v(s[i]);
        else total+=v(s[i]);
    }
    printf("%ld\n", total);
    return 0;
}
