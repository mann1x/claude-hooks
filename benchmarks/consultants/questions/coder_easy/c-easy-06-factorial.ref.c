#include <stdio.h>
int main(void) {
    int n;
    if (scanf("%d", &n) != 1) return 0;
    long long r = 1;
    for (int i = 2; i <= n; i++) r *= i;
    printf("%lld\n", r);
    return 0;
}
