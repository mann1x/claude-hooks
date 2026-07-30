#include <stdio.h>
int main(void) {
    long long x;
    int n = 0;
    while (scanf("%lld", &x) == 1)
        if (x % 2 == 0) n++;
    printf("%d\n", n);
    return 0;
}
