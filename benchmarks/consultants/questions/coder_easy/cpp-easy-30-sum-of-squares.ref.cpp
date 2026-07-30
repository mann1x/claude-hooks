#include <iostream>
int main() {
    long long x, sum = 0;
    while (std::cin >> x) sum += x * x;
    std::cout << sum << std::endl;
    return 0;
}
