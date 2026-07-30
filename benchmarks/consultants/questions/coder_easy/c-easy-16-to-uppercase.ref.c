#include <stdio.h>
#include <string.h>
#include <ctype.h>
int main(void) {
    char buf[100000];
    if (!fgets(buf, sizeof buf, stdin)) buf[0] = 0;
    size_t n = strlen(buf);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = 0;
    for (size_t i = 0; i < n; i++) buf[i] = toupper((unsigned char)buf[i]);
    printf("%s\n", buf);
    return 0;
}
