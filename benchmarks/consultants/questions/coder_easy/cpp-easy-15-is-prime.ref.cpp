#include <iostream>
int main() {
    long long n;
    std::cin >> n;
    bool p = n >= 2;
    for (long long i = 2; i * i <= n; i++)
        if (n % i == 0) { p = false; break; }
    std::cout << (p ? "yes" : "no") << std::endl;
    return 0;
}
