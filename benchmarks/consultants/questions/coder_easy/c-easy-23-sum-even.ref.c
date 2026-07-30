#include <stdio.h>
int main(void) {
    long long x, sum = 0;
    while (scanf("%lld", &x) == 1)
        if (x % 2 == 0) sum += x;
    printf("%lld\n", sum);
    return 0;
}
