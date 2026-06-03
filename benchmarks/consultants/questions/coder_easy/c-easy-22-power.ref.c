#include <stdio.h>
int main(void) {
    long long a, b;
    if (scanf("%lld %lld", &a, &b) != 2) return 0;
    long long r = 1;
    for (long long i = 0; i < b; i++) r *= a;
    printf("%lld\n", r);
    return 0;
}
