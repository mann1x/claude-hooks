#include <stdio.h>
#include <string.h>
int main(void){
    static char s[1000000];
    if(!fgets(s, sizeof(s), stdin)){ printf("YES\n"); return 0; }
    int n=(int)strlen(s);
    while(n>0 && (s[n-1]=='\n'||s[n-1]=='\r')) s[--n]=0;
    static char st[1000000];
    int top=0, ok=1;
    for(int i=0;i<n;i++){
        char c=s[i];
        if(c=='('||c=='['||c=='{') st[top++]=c;
        else if(c==')'||c==']'||c=='}'){
            char m = c==')'?'(':(c==']'?'[':'{');
            if(top==0 || st[--top]!=m){ ok=0; break; }
        }
    }
    printf("%s\n", (ok && top==0) ? "YES" : "NO");
    return 0;
}
