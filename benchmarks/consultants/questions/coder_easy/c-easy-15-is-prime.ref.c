#include <stdio.h>
int main(void) {
    long long n;
    if (scanf("%lld", &n) != 1) return 0;
    int p = n >= 2;
    for (long long i = 2; i * i <= n; i++)
        if (n % i == 0) { p = 0; break; }
    printf("%s\n", p ? "yes" : "no");
    return 0;
}
