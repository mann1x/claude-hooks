#include <stdio.h>
int main(void) {
    char buf[1 << 16];
    if (scanf("%65535s", buf) != 1) { printf("0\n"); return 0; }
    long sum = 0;
    for (int i = 0; buf[i]; i++)
        if (buf[i] >= '0' && buf[i] <= '9') sum += buf[i] - '0';
    printf("%ld\n", sum);
    return 0;
}
