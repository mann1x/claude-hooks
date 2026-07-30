#include <stdio.h>
#include <ctype.h>
int main(void) {
    char w[65536];
    int first = 1;
    while (scanf("%65535s", w) == 1) {
        if (!first) putchar(' ');
        first = 0;
        for (int i = 0; w[i]; i++) {
            int ch = (i == 0) ? toupper((unsigned char)w[i])
                              : tolower((unsigned char)w[i]);
            putchar(ch);
        }
    }
    putchar('\n');
    return 0;
}
