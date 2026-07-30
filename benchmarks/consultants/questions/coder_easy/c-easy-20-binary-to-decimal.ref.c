#include <stdio.h>
int main(void) {
    char buf[1 << 16];
    if (scanf("%65535s", buf) != 1) { printf("0\n"); return 0; }
    long long v = 0;
    for (int i = 0; buf[i]; i++) v = v * 2 + (buf[i] - '0');
    printf("%lld\n", v);
    return 0;
}
