#include <stdio.h>
int main(void) {
    unsigned long long n;
    if (scanf("%llu", &n) != 1) return 0;
    if (n == 0) { printf("0\n"); return 0; }
    char buf[70];
    int i = 0;
    while (n > 0) { buf[i++] = '0' + (n & 1); n >>= 1; }
    while (i > 0) putchar(buf[--i]);
    putchar('\n');
    return 0;
}
