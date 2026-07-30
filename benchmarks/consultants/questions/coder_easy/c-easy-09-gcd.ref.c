#include <stdio.h>
int main(void) {
    long long a, b;
    if (scanf("%lld %lld", &a, &b) != 2) return 0;
    while (b) { long long t = b; b = a % b; a = t; }
    printf("%lld\n", a);
    return 0;
}
