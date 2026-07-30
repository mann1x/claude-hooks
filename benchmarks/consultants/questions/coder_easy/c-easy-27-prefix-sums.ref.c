#include <stdio.h>
int main(void) {
    long long x, acc = 0;
    int first = 1;
    while (scanf("%lld", &x) == 1) {
        acc += x;
        printf(first ? "%lld" : " %lld", acc);
        first = 0;
    }
    printf("\n");
    return 0;
}
