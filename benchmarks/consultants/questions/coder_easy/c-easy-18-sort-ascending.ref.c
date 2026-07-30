#include <stdio.h>
#include <stdlib.h>
static int cmp(const void *a, const void *b) {
    long long x = *(const long long *)a, y = *(const long long *)b;
    return (x > y) - (x < y);
}
int main(void) {
    long long arr[100000];
    int n = 0;
    while (n < 100000 && scanf("%lld", &arr[n]) == 1) n++;
    qsort(arr, n, sizeof(long long), cmp);
    for (int i = 0; i < n; i++) printf(i ? " %lld" : "%lld", arr[i]);
    printf("\n");
    return 0;
}
