#include <iostream>
int main() {
    long long x, sum = 0;
    long count = 0;
    while (std::cin >> x) { sum += x; count++; }
    std::cout << (sum / count) << std::endl;
    return 0;
}
