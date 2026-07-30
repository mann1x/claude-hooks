#include <stdio.h>
int main(void) {
    long long x, m;
    if (scanf("%lld", &m) != 1) return 0;
    while (scanf("%lld", &x) == 1)
        if (x < m) m = x;
    printf("%lld\n", m);
    return 0;
}
