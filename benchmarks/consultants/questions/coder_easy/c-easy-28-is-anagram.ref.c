#include <stdio.h>
#include <string.h>
#include <stdlib.h>
static int cmpc(const void *a, const void *b) {
    return (int)(*(const unsigned char *)a) - (int)(*(const unsigned char *)b);
}
static size_t rd(char *buf) {
    if (!fgets(buf, 100000, stdin)) buf[0] = 0;
    size_t n = strlen(buf);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = 0;
    return n;
}
int main(void) {
    char a[100000], b[100000];
    size_t na = rd(a), nb = rd(b);
    qsort(a, na, 1, cmpc);
    qsort(b, nb, 1, cmpc);
    printf("%s\n", (na == nb && memcmp(a, b, na) == 0) ? "yes" : "no");
    return 0;
}
