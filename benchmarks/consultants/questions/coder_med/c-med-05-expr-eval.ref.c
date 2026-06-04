#include <stdio.h>
#include <string.h>
#include <ctype.h>
int main(void){
    char s[100000];
    if(!fgets(s, sizeof(s), stdin)) return 0;
    long nums[50000]; char ops[50000];
    int nn=0, no=0;
    int i=0, n=(int)strlen(s);
    long cur=0; int have=0;
    while(i<n){
        char c=s[i];
        if(isdigit((unsigned char)c)){ cur=cur*10+(c-'0'); have=1; i++; }
        else if(c=='+'||c=='-'||c=='*'){
            nums[nn++]=cur; cur=0; have=0; ops[no++]=c; i++;
        } else i++;
    }
    if(have || nn==0) nums[nn++]=cur;
    /* collapse * */
    long rnums[50000]; char rops[50000];
    int rn=0, ro=0;
    rnums[rn++]=nums[0];
    for(int k=0;k<no;k++){
        if(ops[k]=='*') rnums[rn-1]=rnums[rn-1]*nums[k+1];
        else { rops[ro++]=ops[k]; rnums[rn++]=nums[k+1]; }
    }
    long total=rnums[0];
    for(int k=0;k<ro;k++){
        if(rops[k]=='+') total+=rnums[k+1]; else total-=rnums[k+1];
    }
    printf("%ld\n", total);
    return 0;
}
