#include <stdio.h>
#include <string.h>
int main(void) {
    char w[65536], best[65536];
    best[0] = 0;
    while (scanf("%65535s", w) == 1)
        if (strlen(w) > strlen(best)) strcpy(best, w);
    printf("%s\n", best);
    return 0;
}
