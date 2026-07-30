#include <stdio.h>
#include <string.h>
int main(void) {
    char buf[100000];
    if (!fgets(buf, sizeof buf, stdin)) buf[0] = 0;
    size_t n = strlen(buf);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r'))
        buf[--n] = 0;
    for (size_t i = 0; i < n / 2; i++) {
        char t = buf[i]; buf[i] = buf[n-1-i]; buf[n-1-i] = t;
    }
    printf("%s\n", buf);
    return 0;
}
