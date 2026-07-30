#include <stdio.h>
int main(void) {
    long c;
    if (scanf("%ld", &c) != 1) return 0;
    printf("%ld\n", c * 9 / 5 + 32);
    return 0;
}
