#include <iostream>
int main() {
    int n;
    std::cin >> n;
    long long r = 1;
    for (int i = 2; i <= n; i++) r *= i;
    std::cout << r << std::endl;
    return 0;
}
