#include <stdio.h>
#include <limits.h>
int main(void) {
    long long x, max1 = LLONG_MIN, max2 = LLONG_MIN;
    int any = 0;
    long long arr[100000];
    int n = 0;
    while (scanf("%lld", &x) == 1 && n < 100000) arr[n++] = x;
    for (int i = 0; i < n; i++) if (arr[i] > max1) max1 = arr[i];
    for (int i = 0; i < n; i++)
        if (arr[i] < max1 && arr[i] > max2) { max2 = arr[i]; any = 1; }
    (void)any;
    printf("%lld\n", max2);
    return 0;
}
