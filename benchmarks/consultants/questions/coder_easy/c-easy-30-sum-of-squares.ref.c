#include <stdio.h>
int main(void) {
    long long x, sum = 0;
    while (scanf("%lld", &x) == 1) sum += x * x;
    printf("%lld\n", sum);
    return 0;
}
