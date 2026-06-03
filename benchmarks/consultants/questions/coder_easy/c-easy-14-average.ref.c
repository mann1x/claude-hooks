#include <stdio.h>
int main(void) {
    long long x, sum = 0;
    long count = 0;
    while (scanf("%lld", &x) == 1) { sum += x; count++; }
    printf("%lld\n", sum / count);
    return 0;
}
