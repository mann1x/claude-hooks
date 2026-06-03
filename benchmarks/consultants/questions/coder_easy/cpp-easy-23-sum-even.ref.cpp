#include <iostream>
int main() {
    long long x, sum = 0;
    while (std::cin >> x)
        if (x % 2 == 0) sum += x;
    std::cout << sum << std::endl;
    return 0;
}
