#include <iostream>
int main() {
    long long x, acc = 0;
    bool first = true;
    while (std::cin >> x) {
        acc += x;
        if (!first) std::cout << ' ';
        first = false;
        std::cout << acc;
    }
    std::cout << std::endl;
    return 0;
}
