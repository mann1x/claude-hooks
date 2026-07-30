#include <stdio.h>
int main(void) {
    long long n, s = 0;
    while (scanf("%lld", &n) == 1) s += n;
    printf("%lld\n", s);
    return 0;
}
