#include <stdio.h>
int main(void) {
    long long arr[100000];
    int n = 0;
    long long x;
    int first = 1;
    while (n < 100000 && scanf("%lld", &x) == 1) {
        int dup = 0;
        for (int i = 0; i < n; i++) if (arr[i] == x) { dup = 1; break; }
        if (!dup) {
            arr[n++] = x;
            printf(first ? "%lld" : " %lld", x);
            first = 0;
        }
    }
    printf("\n");
    return 0;
}
