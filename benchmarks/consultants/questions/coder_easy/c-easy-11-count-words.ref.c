#include <stdio.h>
int main(void) {
    char buf[1 << 16];
    int count = 0;
    while (scanf("%65535s", buf) == 1) count++;
    printf("%d\n", count);
    return 0;
}
