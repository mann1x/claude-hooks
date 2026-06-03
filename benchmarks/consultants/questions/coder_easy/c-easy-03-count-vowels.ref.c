#include <stdio.h>
#include <ctype.h>
int main(void) {
    int ch, n = 0;
    while ((ch = getchar()) != EOF) {
        int l = tolower(ch);
        if (l=='a'||l=='e'||l=='i'||l=='o'||l=='u') n++;
    }
    printf("%d\n", n);
    return 0;
}
